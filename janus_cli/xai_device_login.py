"""xAI Grok OAuth device-code login (RFC 8628).

xAI's OIDC discovery (``https://auth.x.ai/.well-known/openid-configuration``)
advertises ``device_authorization_endpoint`` and the
``urn:ietf:params:oauth:grant-type:device_code`` grant for the public client
Janus uses. The device flow shows a short code, opens (or prints) the xAI
verification page, and polls the token endpoint until the user approves. There
is no local callback listener and no browser redirect back to 127.0.0.1, which
is where the loopback flow kept failing (popups that never reached Janus,
callbacks without a ``code``, ports held by an earlier attempt).

The loopback PKCE flow in ``janus_cli.auth`` stays available behind
``--loopback`` / ``--manual-paste``.
"""

from __future__ import annotations

import logging
import os
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import httpx

from janus_cli import auth as _auth
from janus_cli.auth import AuthError

logger = logging.getLogger(__name__)

XAI_OAUTH_DEVICE_CODE_URL = f"{_auth.XAI_OAUTH_ISSUER}/oauth2/device/code"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_CODE_SOURCE = "oauth-device-code"

_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}
_REQUIRED_DEVICE_FIELDS = (
    "device_code",
    "user_code",
    "verification_uri",
    "expires_in",
    "interval",
)
# RFC 8628 §3.5: on ``slow_down`` the client adds 5 seconds to its interval.
_SLOW_DOWN_STEP_SECONDS = 5
_MAX_INTERVAL_SECONDS = 60
# Cloudflare sits in front of auth.x.ai and can answer a single poll with an
# HTML 403/5xx. Keep polling through a few of those, then give up.
_MAX_EDGE_ERRORS = 5


def _err(message: str, code: str) -> AuthError:
    return AuthError(message, provider="xai-oauth", code=code)


def request_xai_device_code(
    client: httpx.Client,
    *,
    device_endpoint: str = XAI_OAUTH_DEVICE_CODE_URL,
    scope: str = _auth.XAI_OAUTH_SCOPE,
) -> Dict[str, Any]:
    """Ask xAI for a device code + user code."""
    try:
        response = client.post(
            device_endpoint,
            headers=_FORM_HEADERS,
            data={"client_id": _auth.XAI_OAUTH_CLIENT_ID, "scope": scope},
        )
    except httpx.HTTPError as exc:
        raise _err(f"xAI device-code request failed: {exc}", "xai_device_code_request_failed") from exc
    if response.status_code != 200:
        raise _err(
            f"xAI device-code request failed (HTTP {response.status_code}).",
            "xai_device_code_request_failed",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise _err("xAI device-code response was not JSON.", "xai_device_code_invalid") from exc
    if not isinstance(payload, dict):
        raise _err("xAI device-code response was not a JSON object.", "xai_device_code_invalid")
    missing = [key for key in _REQUIRED_DEVICE_FIELDS if not payload.get(key)]
    if missing:
        raise _err(
            f"xAI device-code response is missing: {', '.join(missing)}.",
            "xai_device_code_invalid",
        )
    return payload


def poll_xai_device_token(
    client: httpx.Client,
    *,
    token_endpoint: str,
    device_code: str,
    expires_in: int,
    interval: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Poll the token endpoint until the user approves, denies, or the code expires."""
    deadline = clock() + max(1, int(expires_in))
    current_interval = max(1, min(int(interval), _MAX_INTERVAL_SECONDS))
    edge_errors = 0

    while True:
        if should_cancel is not None and should_cancel():
            raise _err("xAI sign-in was cancelled.", "xai_device_login_cancelled")
        if clock() + current_interval > deadline:
            raise _err(
                "Timed out waiting for xAI device authorization.",
                "xai_device_code_timeout",
            )
        sleep(current_interval)
        if should_cancel is not None and should_cancel():
            raise _err("xAI sign-in was cancelled.", "xai_device_login_cancelled")

        try:
            response = client.post(
                token_endpoint,
                headers=_FORM_HEADERS,
                data={
                    "grant_type": DEVICE_CODE_GRANT_TYPE,
                    "client_id": _auth.XAI_OAUTH_CLIENT_ID,
                    "device_code": device_code,
                },
            )
        except httpx.HTTPError as exc:
            edge_errors += 1
            if edge_errors > _MAX_EDGE_ERRORS:
                raise _err(f"xAI device-code polling failed: {exc}", "xai_device_token_failed") from exc
            continue

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code == 200 and isinstance(payload, dict):
            if not str(payload.get("access_token") or "").strip():
                raise _err("xAI did not return an access_token.", "xai_device_token_invalid")
            if not str(payload.get("refresh_token") or "").strip():
                raise _err("xAI did not return a refresh_token.", "xai_device_token_invalid")
            return payload

        if not isinstance(payload, dict):
            # HTML / empty body from the edge (Cloudflare), not the OAuth server.
            edge_errors += 1
            if edge_errors > _MAX_EDGE_ERRORS:
                raise _err(
                    f"xAI device-code polling kept failing (HTTP {response.status_code}).",
                    "xai_device_token_failed",
                )
            continue

        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            current_interval = min(current_interval + _SLOW_DOWN_STEP_SECONDS, _MAX_INTERVAL_SECONDS)
            continue
        if error == "access_denied":
            raise _err("xAI sign-in was declined.", "xai_device_access_denied")
        if error == "expired_token":
            raise _err("The xAI sign-in code expired. Start the sign-in again.", "xai_device_code_expired")
        detail = payload.get("error_description") or error or f"HTTP {response.status_code}"
        raise _err(f"xAI device-code polling failed: {detail}", "xai_device_token_failed")


def tokens_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Token dict in the shape ``_save_xai_oauth_tokens`` stores."""
    return {
        "access_token": str(payload.get("access_token", "") or "").strip(),
        "refresh_token": str(payload.get("refresh_token", "") or "").strip(),
        "id_token": str(payload.get("id_token", "") or "").strip(),
        "expires_in": payload.get("expires_in"),
        "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
    }


def inference_base_url() -> str:
    return _auth._xai_validate_inference_base_url(
        os.getenv("JANUS_XAI_BASE_URL", "").strip().rstrip("/")
        or os.getenv("XAI_BASE_URL", "").strip().rstrip("/"),
        fallback=_auth.DEFAULT_XAI_OAUTH_BASE_URL,
    )


def utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def xai_device_code_login(
    *,
    timeout_seconds: float = 20.0,
    open_browser: bool = True,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Run the interactive device-code login and return credentials.

    The result has the same shape as ``_xai_oauth_loopback_login``'s so the
    callers save it the same way.
    """
    discovery = _auth._xai_oauth_discovery(timeout_seconds)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(max(20.0, float(timeout_seconds))))
    try:
        device = request_xai_device_code(client)
        verification_url = str(device.get("verification_uri_complete") or device["verification_uri"])
        user_code = str(device["user_code"])

        print("To sign in to xAI Grok, open this page and confirm the code:")
        print(f"  {verification_url}")
        if verification_url != device["verification_uri"]:
            print(f"  (or go to {device['verification_uri']} and enter the code)")
        print(f"  Code: {user_code}")
        print()

        if open_browser and not _auth._is_remote_session() and _auth._can_open_graphical_browser():
            try:
                opened = webbrowser.open(verification_url)
            except Exception:
                opened = False
            if opened:
                print("Browser opened. Check that the code matches, then approve.")
        print(f"Waiting for approval (code expires in {int(device['expires_in']) // 60} min)...")

        payload = poll_xai_device_token(
            client,
            token_endpoint=discovery["token_endpoint"],
            device_code=str(device["device_code"]),
            expires_in=int(device["expires_in"]),
            interval=int(device["interval"]),
            sleep=sleep,
            clock=clock,
        )
    finally:
        if owns_client:
            client.close()

    return {
        "tokens": tokens_from_payload(payload),
        "discovery": discovery,
        "redirect_uri": "",
        "base_url": inference_base_url(),
        "last_refresh": utc_now_z(),
        "source": DEVICE_CODE_SOURCE,
    }
