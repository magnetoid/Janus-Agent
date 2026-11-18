from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple, Union
import janus_cli.web_server as web_server_mod

router = APIRouter()

@router.get("/api/model/info")
def get_model_info():
    """Return resolved model metadata for the currently configured model.

    Calls the same context-length resolution chain the agent uses, so the
    frontend can display "Auto-detected: 200K" alongside the override field.
    Also returns model capabilities (vision, reasoning, tools) when available.
    """
    try:
        cfg = web_server_mod.load_config()
        model_cfg = cfg.get("model", "")

        # Extract model name and provider from the config
        if isinstance(model_cfg, dict):
            model_name = model_cfg.get("default", model_cfg.get("name", ""))
            provider = model_cfg.get("provider", "")
            base_url = model_cfg.get("base_url", "")
            config_ctx = model_cfg.get("context_length")
        else:
            model_name = str(model_cfg) if model_cfg else ""
            provider = ""
            base_url = ""
            config_ctx = None

        if not model_name:
            return dict(web_server_mod._EMPTY_MODEL_INFO, provider=provider)

        # Resolve auto-detected context length (pass config_ctx=None to get
        # purely auto-detected value, then separately report the override)
        try:
            from agent.model_metadata import get_model_context_length
            auto_ctx = get_model_context_length(
                model=model_name,
                base_url=base_url,
                provider=provider,
                config_context_length=None,  # ignore override — we want auto value
            )
        except Exception:
            auto_ctx = 0

        config_ctx_int = 0
        if isinstance(config_ctx, int) and config_ctx > 0:
            config_ctx_int = config_ctx

        # Effective is what the agent actually uses
        effective_ctx = config_ctx_int if config_ctx_int > 0 else auto_ctx

        # Try to get model capabilities from models.dev
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        return {
            "model": model_name,
            "provider": provider,
            "auto_context_length": auto_ctx,
            "config_context_length": config_ctx_int,
            "effective_context_length": effective_ctx,
            "capabilities": caps,
        }
    except Exception:
        web_server_mod._log.exception("GET /api/model/info failed")
        return dict(web_server_mod._EMPTY_MODEL_INFO)

@router.get("/api/model/options")
def get_model_options():
    """Return authenticated providers + their curated model lists.

    REST equivalent of the ``model.options`` JSON-RPC on tui_gateway, so the
    dashboard Models page can render the picker without a live chat session.
    The response shape matches ``model.options`` 1:1 so ``ModelPickerDialog``
    can share the same types.
    """
    try:
        from janus_cli.inventory import build_models_payload, load_picker_context

        # include_unconfigured + picker_hints + canonical_order mirror the
        # tui_gateway `model.options` JSON-RPC handler exactly, so every GUI
        # surface fed by this endpoint (Settings → Model, the first-run
        # onboarding picker) sees the SAME full provider universe `janus model`
        # exposes — not just the authenticated subset. Unconfigured providers
        # come back as skeleton rows carrying `authenticated=False` +
        # `auth_type`/`key_env`/`warning` so the GUI can render a setup
        # affordance instead of hiding the provider entirely.
        return build_models_payload(
            load_picker_context(),
            max_models=50,
            include_unconfigured=True,
            picker_hints=True,
            canonical_order=True,
            pricing=True,
            capabilities=True,
        )
    except Exception:
        web_server_mod._log.exception("GET /api/model/options failed")
        raise web_server_mod.HTTPException(status_code=500, detail="Failed to list model options")

@router.get("/api/model/recommended-default")
def get_recommended_default_model(provider: str = ""):
    """Return the recommended default model for a freshly-authenticated provider.

    Returns the first curated model for the provider, matching the model
    curation `janus model` does.

    Response: {"provider": str, "model": str}. `model` may be empty if
    nothing could be resolved (caller degrades gracefully).
    """
    slug = (provider or "").strip().lower()

    try:
        from janus_cli.inventory import build_models_payload, load_picker_context

        payload = build_models_payload(load_picker_context(), max_models=50)
        for row in payload.get("providers", []):
            if str(row.get("slug", "")).lower() == slug:
                models = row.get("models") or []
                return {"provider": slug, "model": models[0] if models else ""}
        return {"provider": slug, "model": ""}
    except Exception:
        web_server_mod._log.exception("GET /api/model/recommended-default failed")
        return {"provider": slug, "model": ""}

@router.get("/api/model/auxiliary")
def get_auxiliary_models():
    """Return current auxiliary task assignments.

    Shape:
      {
        "tasks": [
          {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
          ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
      }
    """
    try:
        cfg = web_server_mod.load_config()
        aux_cfg = cfg.get("auxiliary", {})
        if not isinstance(aux_cfg, dict):
            aux_cfg = {}

        tasks = []
        for slot in web_server_mod._AUX_TASK_SLOTS:
            slot_cfg = aux_cfg.get(slot, {}) if isinstance(aux_cfg.get(slot), dict) else {}
            tasks.append({
                "task": slot,
                "provider": str(slot_cfg.get("provider", "auto") or "auto"),
                "model": str(slot_cfg.get("model", "") or ""),
                "base_url": str(slot_cfg.get("base_url", "") or ""),
            })

        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            main = {
                "provider": str(model_cfg.get("provider", "") or ""),
                "model": str(model_cfg.get("default", model_cfg.get("name", "")) or ""),
            }
        else:
            main = {"provider": "", "model": str(model_cfg) if model_cfg else ""}

        return {"tasks": tasks, "main": main}
    except Exception:
        web_server_mod._log.exception("GET /api/model/auxiliary failed")
        raise web_server_mod.HTTPException(status_code=500, detail="Failed to read auxiliary config")

@router.post("/api/model/set")
async def set_model_assignment(body: web_server_mod.ModelAssignment):
    """Assign a model to the main slot or an auxiliary task slot.

    Writes to ``~/.janus/config.yaml`` — applies to **new** sessions only.
    The currently running chat PTY (if any) is not affected; use the
    ``/model`` slash command inside a chat to hot-swap that specific session.
    """
    scope = (body.scope or "").strip().lower()
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    task = (body.task or "").strip().lower()
    base_url = (body.base_url or "").strip()

    if scope not in {"main", "auxiliary"}:
        raise web_server_mod.HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    try:
        cfg = web_server_mod.load_config()

        if scope == "main":
            if not provider or not model:
                raise web_server_mod.HTTPException(status_code=400, detail="provider and model required for main")
            model_cfg = web_server_mod._apply_main_model_assignment(
                cfg.get("model", {}), provider, model, base_url
            )
            cfg["model"] = model_cfg

            web_server_mod.save_config(cfg)

            # Surface auxiliary slots still pinned to a *different* provider than
            # the new main one. Switching the main model does NOT touch aux pins
            # (they're independent, sticky per-task overrides — see
            # auxiliary_client._resolve_auto). A user who switches main away from
            # a now-unpaid provider (e.g. one with a $0 balance) keeps paying 402s
            # on every background aux call until they reset those pins. We never
            # auto-clear them — pinning aux to a cheaper/different model is a
            # legitimate config — but we tell the caller so the UI can offer a
            # "reset to main" nudge instead of silently burning credits.
            new_provider = provider.strip().lower()
            stale_aux: list[dict] = []
            aux_cfg = cfg.get("auxiliary", {})
            if isinstance(aux_cfg, dict):
                for slot in web_server_mod._AUX_TASK_SLOTS:
                    slot_cfg = aux_cfg.get(slot)
                    if not isinstance(slot_cfg, dict):
                        continue
                    slot_provider = str(slot_cfg.get("provider", "") or "").strip()
                    if (
                        slot_provider
                        and slot_provider.lower() not in {"auto", ""}
                        and slot_provider.lower() != new_provider
                    ):
                        stale_aux.append({
                            "task": slot,
                            "provider": slot_provider,
                            "model": str(slot_cfg.get("model", "") or ""),
                        })

            return {
                "ok": True,
                "scope": "main",
                "provider": provider,
                "model": model,
                "base_url": model_cfg.get("base_url", ""),
                "stale_aux": stale_aux,
            }

        # scope == "auxiliary"
        aux = cfg.get("auxiliary")
        if not isinstance(aux, dict):
            aux = {}

        if task == "__reset__":
            # Reset every slot to provider="auto", model="" — keeps other fields intact.
            for slot in web_server_mod._AUX_TASK_SLOTS:
                slot_cfg = aux.get(slot)
                if not isinstance(slot_cfg, dict):
                    slot_cfg = {}
                slot_cfg["provider"] = "auto"
                slot_cfg["model"] = ""
                aux[slot] = slot_cfg
            cfg["auxiliary"] = aux
            web_server_mod.save_config(cfg)
            return {"ok": True, "scope": "auxiliary", "reset": True}

        if not provider:
            raise web_server_mod.HTTPException(status_code=400, detail="provider required for auxiliary")

        targets = [task] if task else list(web_server_mod._AUX_TASK_SLOTS)
        for slot in targets:
            if slot not in web_server_mod._AUX_TASK_SLOTS:
                raise web_server_mod.HTTPException(status_code=400, detail=f"unknown auxiliary task: {slot}")
            slot_cfg = aux.get(slot)
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = provider
            slot_cfg["model"] = model
            aux[slot] = slot_cfg

        cfg["auxiliary"] = aux
        web_server_mod.save_config(cfg)
        return {
            "ok": True,
            "scope": "auxiliary",
            "tasks": targets,
            "provider": provider,
            "model": model,
        }
    except web_server_mod.HTTPException:
        raise
    except Exception:
        web_server_mod._log.exception("POST /api/model/set failed")
        raise web_server_mod.HTTPException(status_code=500, detail="Failed to save model assignment")

