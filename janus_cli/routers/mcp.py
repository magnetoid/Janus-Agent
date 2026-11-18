from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple, Union
import janus_cli.web_server as web_server_mod

router = APIRouter()

@router.get("/api/mcp/servers")
async def list_mcp_servers():
    from janus_cli.mcp_config import _get_mcp_servers

    servers = _get_mcp_servers()
    return {
        "servers": [
            web_server_mod._mcp_server_summary(name, cfg) for name, cfg in sorted(servers.items())
        ]
    }

@router.post("/api/mcp/servers")
async def add_mcp_server(body: web_server_mod.MCPServerCreate):
    from janus_cli.mcp_config import _get_mcp_servers, _save_mcp_server

    name = (body.name or "").strip()
    if not name:
        raise web_server_mod.HTTPException(status_code=400, detail="Server name is required")
    if name in _get_mcp_servers():
        raise web_server_mod.HTTPException(status_code=409, detail=f"Server '{name}' already exists")
    if not body.url and not body.command:
        raise web_server_mod.HTTPException(
            status_code=400,
            detail="Provide either a URL (HTTP/SSE server) or a command (stdio server)",
        )

    server_config: web_server_mod.Dict[str, web_server_mod.Any] = {}
    if body.url:
        server_config["url"] = body.url.strip()
    if body.command:
        server_config["command"] = body.command.strip()
        if body.args:
            server_config["args"] = list(body.args)
    if body.env:
        server_config["env"] = dict(body.env)
    if body.auth:
        server_config["auth"] = body.auth

    try:
        _save_mcp_server(name, server_config)
    except Exception as exc:
        web_server_mod._log.exception("POST /api/mcp/servers failed")
        raise web_server_mod.HTTPException(status_code=400, detail=str(exc)) from exc

    return web_server_mod._mcp_server_summary(name, server_config)

@router.delete("/api/mcp/servers/{name}")
async def remove_mcp_server(name: str):
    from janus_cli.mcp_config import _remove_mcp_server

    if not _remove_mcp_server(name):
        raise web_server_mod.HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"ok": True}

@router.post("/api/mcp/servers/{name}/test")
async def test_mcp_server(name: str):
    """Connect to the server, list its tools, disconnect.  Returns tool list."""
    from janus_cli.mcp_config import _get_mcp_servers, _probe_single_server

    servers = _get_mcp_servers()
    if name not in servers:
        raise web_server_mod.HTTPException(status_code=404, detail=f"Server '{name}' not found")

    try:
        # Probe blocks on a dedicated MCP event loop — run in a thread so the
        # FastAPI event loop is never blocked.
        tools = await web_server_mod.asyncio.to_thread(_probe_single_server, name, servers[name])
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "tools": [],
        }
    return {
        "ok": True,
        "tools": [{"name": t, "description": d} for t, d in tools],
    }

@router.put("/api/mcp/servers/{name}/enabled")
async def set_mcp_server_enabled(name: str, body: web_server_mod.MCPEnabledToggle):
    """Enable or disable an MCP server (takes effect on next session/gateway).

    Toggles the ``enabled`` key on the server's config.yaml entry — the same
    flag the agent reads at startup.  Disabled servers stay in config so they
    can be re-enabled without re-entering their settings.
    """
    cfg = web_server_mod.load_config()
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict) or name not in servers:
        raise web_server_mod.HTTPException(status_code=404, detail=f"Server '{name}' not found")
    if not isinstance(servers[name], dict):
        raise web_server_mod.HTTPException(status_code=400, detail="Malformed server config")
    servers[name]["enabled"] = bool(body.enabled)
    web_server_mod.save_config(cfg)
    return {"ok": True, "name": name, "enabled": bool(body.enabled)}

@router.get("/api/mcp/catalog")
async def list_mcp_catalog():
    """Browse the bundled MCP catalog (the optional-mcps/ manifests).

    Each entry reports whether it's already installed and enabled so the UI
    can show install / enabled state inline.  This is the same catalog
    `janus mcp catalog` / `janus mcp install` read.
    """
    try:
        from janus_cli import mcp_catalog
    except Exception as exc:
        web_server_mod._log.exception("mcp_catalog import failed")
        raise web_server_mod.HTTPException(status_code=500, detail=f"Catalog unavailable: {exc}")

    entries = []
    try:
        for entry in mcp_catalog.list_catalog():
            auth = entry.auth
            entries.append({
                "name": entry.name,
                "description": entry.description,
                "source": entry.source,
                "transport": entry.transport.type,
                "auth_type": getattr(auth, "type", "none"),
                # Env vars the user must supply (names + prompts only, never values).
                "required_env": [
                    {"name": e.name, "prompt": e.prompt, "required": e.required}
                    for e in getattr(auth, "env", []) or []
                ],
                "needs_install": entry.install is not None,
                "installed": mcp_catalog.is_installed(entry.name),
                "enabled": mcp_catalog.is_enabled(entry.name),
            })
    except Exception:
        web_server_mod._log.exception("list_mcp_catalog failed")

    diagnostics = []
    try:
        diagnostics = [
            {"name": n, "kind": k, "message": m}
            for (n, k, m) in mcp_catalog.catalog_diagnostics()
        ]
    except Exception:
        pass

    return {"entries": entries, "diagnostics": diagnostics}

@router.post("/api/mcp/catalog/install")
async def install_mcp_catalog_entry(body: web_server_mod.MCPCatalogInstall):
    """Install a catalog MCP into config.yaml.

    For HTTP/stdio entries with required env vars, those are written to .env
    via the standard env path so the agent can read them at session start.
    Entries that need a git bootstrap (``needs_install``) are installed via
    the CLI action path because the clone can take time.
    """
    from janus_cli import mcp_catalog

    name = (body.name or "").strip()
    entry = mcp_catalog.get_entry(name)
    if entry is None:
        raise web_server_mod.HTTPException(status_code=404, detail=f"No catalog entry '{name}'")

    # Persist any supplied env vars first (catalog entries declare which names
    # they need; we only write the ones the user provided).
    if body.env:
        for k, v in body.env.items():
            if v:
                web_server_mod.save_env_value(k, v)

    # Git-bootstrap entries can take a while to clone — run via the background
    # action path so the request returns immediately and the UI can tail logs.
    if entry.install is not None:
        try:
            proc = web_server_mod._spawn_janus_action(["mcp", "install", name], "mcp-install")
        except Exception as exc:
            raise web_server_mod.HTTPException(status_code=500, detail=f"Install failed: {exc}")
        return {"ok": True, "name": name, "background": True, "action": "mcp-install"}

    # No git step — install synchronously via the catalog API.
    try:
        await web_server_mod.asyncio.to_thread(mcp_catalog.install_entry, entry, enable=body.enable)
    except Exception as exc:
        web_server_mod._log.exception("install_mcp_catalog_entry failed")
        raise web_server_mod.HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": name, "background": False}

