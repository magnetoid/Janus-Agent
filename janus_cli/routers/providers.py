from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple, Union
import janus_cli.web_server as web_server_mod

router = APIRouter()

@router.post("/api/providers/validate")
async def validate_provider_credential(body: web_server_mod.EnvVarUpdate, request: web_server_mod.Request):
    """Live-probe a provider credential before it's saved.

    Returns {ok, reachable, message}. ok=True means the provider accepted the
    key; ok=False + reachable=True means the key is bad (caller should block);
    reachable=False means the network probe couldn't run (caller may save with
    a warning rather than hard-blocking offline users).
    """
    web_server_mod._require_token(request)
    import httpx

    key = (body.key or "").strip()
    value = (body.value or "").strip()
    if not value:
        return {"ok": False, "reachable": True, "message": "Enter a value first."}

    # Local / custom endpoint: validate connectivity, not auth — any HTTP
    # response (even 401) proves the endpoint is up. Also surface the model
    # ids the endpoint advertises (OpenAI ``/v1/models`` shape) so the GUI can
    # auto-pick a default without asking the user to type a model name.
    if key == "OPENAI_BASE_URL":
        url = value.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
                resp = await client.get(url)
            return {"ok": True, "reachable": True, "message": "", "models": web_server_mod._parse_model_ids(resp)}
        except Exception:
            return {"ok": False, "reachable": False, "message": f"Could not reach {url}."}

    probe = web_server_mod._CREDENTIAL_PROBES.get(key)
    if not probe:
        # No probe for this provider — can't validate, don't block.
        return {"ok": True, "reachable": False, "message": ""}

    url, auth = probe
    headers = {"Accept": "application/json"}
    params = {}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    else:
        params["key"] = value

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url, headers=headers, params=params)
    except Exception:
        return {"ok": False, "reachable": False, "message": "Could not reach the provider to verify the key."}

    if resp.status_code in (401, 403):
        return {"ok": False, "reachable": True, "message": "That API key was rejected. Double-check it and try again."}
    if resp.status_code == 429 or resp.is_success:
        # 429 = key is valid but rate-limited; success = valid.
        return {"ok": True, "reachable": True, "message": ""}
    return {"ok": False, "reachable": True, "message": f"Provider returned HTTP {resp.status_code} for this key."}

@router.get("/api/providers/oauth")
async def list_oauth_providers():
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external" | "loopback"
        cli_command     fallback CLI command for users to run manually
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("janus_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool
    """
    providers = []
    for p in web_server_mod._OAUTH_PROVIDER_CATALOG:
        status = web_server_mod._resolve_provider_status(p["id"], p.get("status_fn"))
        providers.append({
            "id": p["id"],
            "name": p["name"],
            "flow": p["flow"],
            "cli_command": p["cli_command"],
            "docs_url": p["docs_url"],
            "status": status,
        })
    return {"providers": providers}

@router.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(provider_id: str, request: web_server_mod.Request):
    """Disconnect an OAuth provider. Token-protected (matches /env/reveal)."""
    web_server_mod._require_token(request)

    valid_ids = {p["id"] for p in web_server_mod._OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid_ids:
        raise web_server_mod.HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider_id}. "
                   f"Available: {', '.join(sorted(valid_ids))}",
        )

    # Anthropic and claude-code clear the same Janus-managed PKCE file
    # AND forget the Claude Code import. We don't touch ~/.claude/* directly
    # — that's owned by the Claude Code CLI; users can re-auth there if they
    # want to undo a disconnect.
    if provider_id in {"anthropic", "claude-code"}:
        try:
            from agent.anthropic_adapter import _JANUS_OAUTH_FILE
            if _JANUS_OAUTH_FILE.exists():
                _JANUS_OAUTH_FILE.unlink()
        except Exception:
            pass
        # Also clear the credential pool entry if present.
        try:
            from janus_cli.auth import clear_provider_auth
            clear_provider_auth("anthropic")
        except Exception:
            pass
        web_server_mod._log.info("oauth/disconnect: %s", provider_id)
        return {"ok": True, "provider": provider_id}

    try:
        from janus_cli.auth import clear_provider_auth
        cleared = clear_provider_auth(provider_id)
        web_server_mod._log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
        return {"ok": bool(cleared), "provider": provider_id}
    except Exception as e:
        web_server_mod._log.exception("disconnect %s failed", provider_id)
        raise web_server_mod.HTTPException(status_code=500, detail=str(e))

@router.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(provider_id: str, request: web_server_mod.Request):
    """Initiate an OAuth login flow. Token-protected."""
    web_server_mod._require_token(request)
    web_server_mod._gc_oauth_sessions()
    valid = {p["id"] for p in web_server_mod._OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise web_server_mod.HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in web_server_mod._OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise web_server_mod.HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        # The pkce branch is gated on provider_id == "anthropic" because
        # `_start_anthropic_pkce()` is hardcoded to the Anthropic flow.
        # Routing any other future pkce-flagged provider through it would
        # silently launch the Anthropic OAuth flow (the bug fixed in this
        # change for MiniMax). New PKCE providers must add their own
        # start function and an explicit branch here.
        if catalog_entry["flow"] == "pkce" and provider_id == "anthropic":
            return web_server_mod._start_anthropic_pkce()
        if catalog_entry["flow"] == "device_code":
            return await web_server_mod._start_device_code_flow(provider_id)
    except web_server_mod.HTTPException:
        raise
    except Exception as e:
        web_server_mod._log.exception("oauth/start %s failed", provider_id)
        raise web_server_mod.HTTPException(status_code=500, detail=str(e))
    raise web_server_mod.HTTPException(status_code=400, detail="Unsupported flow")

@router.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(provider_id: str, body: web_server_mod.OAuthSubmitBody, request: web_server_mod.Request):
    """Submit the auth code for PKCE flows. Token-protected."""
    web_server_mod._require_token(request)
    if provider_id == "anthropic":
        return await web_server_mod.asyncio.get_running_loop().run_in_executor(
            None, web_server_mod._submit_anthropic_pkce, body.session_id, body.code,
        )
    raise web_server_mod.HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")

@router.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(provider_id: str, session_id: str):
    """Poll a session's status (no auth — read-only state).

    Shared by the device-code flows (Nous, OpenAI Codex, MiniMax, xAI Grok),
    which all surface progress through the same background-worker-updated
    ``status`` field.
    """
    with web_server_mod._oauth_sessions_lock:
        sess = web_server_mod._oauth_sessions.get(session_id)
    if not sess:
        raise web_server_mod.HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise web_server_mod.HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }

@router.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(session_id: str, request: web_server_mod.Request):
    """Cancel a pending OAuth session. Token-protected."""
    web_server_mod._require_token(request)
    with web_server_mod._oauth_sessions_lock:
        sess = web_server_mod._oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    return {"ok": True, "session_id": session_id}

