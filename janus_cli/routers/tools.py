from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple, Union
import janus_cli.web_server as web_server_mod

router = APIRouter()

@router.get("/api/tools/toolsets")
async def get_toolsets():
    from janus_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
        gui_toolset_label,
    )
    from toolsets import resolve_toolset

    config = web_server_mod.load_config()
    enabled_toolsets = _get_platform_tools(
        config,
        "cli",
        include_default_mcp_servers=False,
    )
    result = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        is_enabled = name in enabled_toolsets
        result.append({
            "name": name,
            "label": gui_toolset_label(label),
            "description": desc,
            "enabled": is_enabled,
            "available": is_enabled,
            "configured": _toolset_has_keys(name, config),
            "tools": tools,
        })
    return result

@router.put("/api/tools/toolsets/{name}")
async def toggle_toolset(name: str, body: web_server_mod.ToolsetToggle):
    """Enable/disable a configurable toolset for the desktop (cli) platform.

    Persists to ``platform_toolsets.cli`` via the same ``_save_platform_tools``
    helper the CLI ``janus tools`` picker uses, so the GUI and CLI stay in
    lockstep. Returns 400 for unknown toolset keys.
    """
    from janus_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _save_platform_tools,
    )

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise web_server_mod.HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    config = web_server_mod.load_config()
    enabled = set(
        _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    )
    if body.enabled:
        enabled.add(name)
    else:
        enabled.discard(name)
    _save_platform_tools(config, "cli", enabled)
    return {"ok": True, "name": name, "enabled": body.enabled}

@router.get("/api/tools/toolsets/{name}/config")
async def get_toolset_config(name: str):
    """Return the provider matrix + key status for a toolset's config panel.

    Surfaces the same provider rows the CLI ``janus tools`` picker shows
    (via ``_visible_providers``), each with its ``env_vars`` annotated with
    current ``is_set`` state so the GUI can render provider selection + key
    entry. Toolsets without a ``TOOL_CATEGORIES`` entry return an empty
    provider list and ``has_category: false``. Returns 400 for unknown keys.
    """
    from janus_cli.tools_config import (
        TOOL_CATEGORIES,
        _get_effective_configurable_toolsets,
        _is_provider_active,
        _visible_providers,
    )
    from janus_cli.config import get_env_value

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise web_server_mod.HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    config = web_server_mod.load_config()
    cat = TOOL_CATEGORIES.get(name)
    providers = []
    active_provider = None
    if cat:
        for prov in _visible_providers(cat, config):
            env_vars = [
                {
                    "key": e["key"],
                    "prompt": e.get("prompt", e["key"]),
                    "url": e.get("url"),
                    "default": e.get("default"),
                    "is_set": bool(get_env_value(e["key"])),
                }
                for e in prov.get("env_vars", [])
            ]
            # Surface the same active-provider determination the CLI picker
            # uses (``_is_provider_active``) so the GUI highlights the provider
            # actually written to config (e.g. web.backend), not just the first
            # keyless one in the list.
            is_active = _is_provider_active(prov, config)
            if is_active and active_provider is None:
                active_provider = prov["name"]
            providers.append({
                "name": prov["name"],
                "badge": prov.get("badge", ""),
                "tag": prov.get("tag", ""),
                "env_vars": env_vars,
                "post_setup": prov.get("post_setup"),
                "is_active": is_active,
            })
    return {
        "name": name,
        "has_category": cat is not None,
        "providers": providers,
        "active_provider": active_provider,
    }

@router.put("/api/tools/toolsets/{name}/provider")
async def select_toolset_provider(name: str, body: web_server_mod.ToolsetProviderSelect):
    """Persist a provider selection for a toolset (no key prompting).

    Delegates to ``apply_provider_selection`` — the shared, non-interactive
    core extracted from the CLI configurator — so the GUI and ``janus tools``
    write identical config keys (``web.backend``, ``tts.provider``, etc.).
    API keys and post-setup flows are handled by separate endpoints. Returns
    400 for unknown toolset or provider names.
    """
    from janus_cli.tools_config import (
        apply_provider_selection,
        _get_effective_configurable_toolsets,
    )

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise web_server_mod.HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    config = web_server_mod.load_config()
    try:
        apply_provider_selection(name, body.provider, config)
    except KeyError as exc:
        raise web_server_mod.HTTPException(status_code=400, detail=str(exc).strip('"'))
    web_server_mod.save_config(config)
    return {"ok": True, "name": name, "provider": body.provider}

@router.put("/api/tools/toolsets/{name}/env")
async def save_toolset_env(name: str, body: web_server_mod.ToolsetEnvUpdate):
    """Persist API keys for a toolset's provider env vars.

    Writes each ``key: value`` to ``~/.janus/.env`` via ``save_env_value`` —
    the same store ``janus tools`` writes when it prompts for keys. Keys are
    validated against the env-var allowlist for the toolset's category (the
    union of every visible provider's ``env_vars``), so the GUI can't write an
    arbitrary env var through this endpoint. A blank value is treated as
    "leave unchanged" and skipped. Returns the saved/skipped key lists and the
    refreshed ``is_set`` status. Returns 400 for unknown toolset or env keys.
    """
    from janus_cli.tools_config import (
        TOOL_CATEGORIES,
        _get_effective_configurable_toolsets,
        _visible_providers,
    )
    from janus_cli.config import get_env_value, save_env_value

    valid_ts = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid_ts:
        raise web_server_mod.HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    config = web_server_mod.load_config()
    cat = TOOL_CATEGORIES.get(name)
    allowed: set[str] = set()
    if cat:
        for prov in _visible_providers(cat, config):
            for e in prov.get("env_vars", []):
                allowed.add(e["key"])

    unknown = [k for k in body.env if k not in allowed]
    if unknown:
        raise web_server_mod.HTTPException(
            status_code=400,
            detail=f"Unknown env var(s) for toolset {name}: {', '.join(sorted(unknown))}",
        )

    saved: web_server_mod.List[str] = []
    skipped: web_server_mod.List[str] = []
    for key, value in body.env.items():
        if value and value.strip():
            try:
                web_server_mod.save_env_value(key, value.strip())
            except ValueError as exc:
                raise web_server_mod.HTTPException(status_code=400, detail=str(exc))
            saved.append(key)
        else:
            skipped.append(key)

    status = {k: bool(get_env_value(k)) for k in allowed}
    return {"ok": True, "name": name, "saved": saved, "skipped": skipped, "is_set": status}

@router.post("/api/tools/toolsets/{name}/post-setup")
async def run_toolset_post_setup(name: str, body: web_server_mod.ToolsetPostSetup):
    """Spawn a provider's post-setup install hook as a background action.

    Post-setup hooks (npm install for browser/Camofox, pip install for
    KittenTTS/Piper/ddgs, cua-driver fetch, etc.) are long-running and
    text-output, so this follows the spawn-action pattern: it launches
    ``janus tools post-setup <key>`` and the frontend tails the log via
    ``GET /api/actions/tools-post-setup/status``. The ``key`` is validated
    against the declared post-setup allowlist before spawning. Returns 400
    for unknown toolset or post-setup key.
    """
    from janus_cli.tools_config import (
        _get_effective_configurable_toolsets,
        valid_post_setup_keys,
    )

    valid_ts = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid_ts:
        raise web_server_mod.HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    if body.key not in valid_post_setup_keys():
        raise web_server_mod.HTTPException(
            status_code=400, detail=f"Unknown post-setup key: {body.key}"
        )

    try:
        proc = web_server_mod._spawn_janus_action(
            ["tools", "post-setup", body.key], "tools-post-setup"
        )
    except Exception as exc:
        web_server_mod._log.exception("Failed to spawn tools post-setup")
        raise web_server_mod.HTTPException(
            status_code=500, detail=f"Failed to run post-setup: {exc}"
        )
    return {"ok": True, "pid": proc.pid, "name": "tools-post-setup", "key": body.key}

