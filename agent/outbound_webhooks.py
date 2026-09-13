"""HMAC-signed outbound lifecycle webhooks.

Posts compact JSON to configured URLs when agent/session/cron events fire.
Never blocks the caller — delivery is fire-and-forget on a daemon thread.
Failures are logged and swallowed so a dead webhook cannot stall the agent.

Config (config.yaml)::

    webhooks:
      outbound:
        enabled: true
        secret: "hex-or-passphrase"   # HMAC-SHA256; empty = unsigned
        urls:
          - https://example.com/hooks/janus
        events:                       # omit / empty = all mapped events
          - session.finalize
          - cron.complete
          - subagent.start
          - subagent.stop

Env overrides (useful for a single receiver without editing yaml):

    JANUS_OUTBOUND_WEBHOOK_URL
    JANUS_OUTBOUND_WEBHOOK_SECRET
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# Plugin hook name -> public event name. Hooks not in this map are ignored
# so pre_tool_call / post_llm_call never flood a receiver.
HOOK_EVENT_MAP: Dict[str, str] = {
    "on_session_start": "session.start",
    "on_session_end": "session.end",
    "on_session_finalize": "session.finalize",
    "subagent_start": "subagent.start",
    "subagent_stop": "subagent.stop",
    "api_request_error": "api.error",
}

_JSON_TYPES = (str, int, float, bool, type(None))
_MAX_STRING = 2000
_POST_TIMEOUT_S = 8.0


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Reduce hook kwargs to JSON-serializable primitives."""
    if depth > 4:
        return str(value)[:_MAX_STRING]
    if isinstance(value, _JSON_TYPES):
        if isinstance(value, str) and len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + "…"
        return value
    if isinstance(value, Mapping):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 40:
                out["_truncated"] = True
                break
            out[str(k)] = _json_safe(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, depth=depth + 1) for v in value[:40]]
    return str(value)[:_MAX_STRING]


def _load_outbound_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    try:
        from janus_cli.config import load_config

        root = load_config() or {}
        webhooks = root.get("webhooks") or {}
        if isinstance(webhooks, dict):
            outbound = webhooks.get("outbound") or {}
            if isinstance(outbound, dict):
                cfg = dict(outbound)
    except Exception:
        logger.debug("outbound webhook config load failed", exc_info=True)

    env_url = (os.getenv("JANUS_OUTBOUND_WEBHOOK_URL") or "").strip()
    env_secret = os.getenv("JANUS_OUTBOUND_WEBHOOK_SECRET")
    if env_url:
        urls = list(cfg.get("urls") or [])
        if env_url not in urls:
            urls.append(env_url)
        cfg["urls"] = urls
        cfg["enabled"] = True
    if env_secret is not None and env_secret != "":
        cfg["secret"] = env_secret
    return cfg


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, headers: Dict[str, str]) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S) as resp:
            # Drain the body so keep-alive sockets can recycle.
            resp.read(256)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("outbound webhook POST to %s failed: %s", url, exc)


def emit(event: str, payload: Optional[Mapping[str, Any]] = None) -> bool:
    """Queue a signed POST for ``event``. Returns True if a delivery was queued."""
    event = (event or "").strip()
    if not event:
        return False

    cfg = _load_outbound_config()
    if not cfg.get("enabled"):
        return False

    allowed = cfg.get("events") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed = [str(e).strip() for e in allowed if str(e).strip()]
    if allowed and event not in allowed:
        return False

    urls = [str(u).strip() for u in (cfg.get("urls") or []) if str(u).strip()]
    if not urls:
        return False

    body_obj = {
        "event": event,
        "ts": time.time(),
        "payload": _json_safe(payload or {}),
    }
    body = json.dumps(body_obj, default=str, separators=(",", ":")).encode("utf-8")
    secret = str(cfg.get("secret") or "")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "janus-outbound-webhook/1",
        "X-Janus-Event": event,
    }
    if secret:
        headers["X-Janus-Signature"] = f"sha256={_sign(secret, body)}"

    for url in urls:
        threading.Thread(
            target=_post,
            args=(url, body, headers),
            name=f"janus-webhook-{event}",
            daemon=True,
        ).start()
    return True


def emit_hook(hook_name: str, **kwargs: Any) -> bool:
    """Fan-out from ``invoke_hook``. Unknown hooks are no-ops."""
    event = HOOK_EVENT_MAP.get(hook_name)
    if not event:
        return False
    return emit(event, kwargs)


def emit_cron_complete(
    *,
    job_id: str,
    name: str = "",
    success: bool,
    error: Optional[str] = None,
) -> bool:
    return emit(
        "cron.complete",
        {
            "job_id": job_id,
            "name": name,
            "success": bool(success),
            "error": error,
        },
    )
