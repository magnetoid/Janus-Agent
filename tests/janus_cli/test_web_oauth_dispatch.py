"""Regression tests for the OAuth dispatcher in janus_cli.web_server.

Bug history (2026-05-09): the `_OAUTH_PROVIDER_CATALOG` had two entries
flagged ``flow: "pkce"`` — anthropic and minimax-oauth — and the
dispatcher ``start_oauth_login`` hardcoded ``_start_anthropic_pkce()``
for any pkce-flagged provider. So clicking "Login" next to MiniMax in
the dashboard's Keys tab silently launched the Anthropic/Claude OAuth
flow.

The fix:
  1. Catalog entry for minimax-oauth changed from ``flow: "pkce"`` to
     ``flow: "device_code"`` (the actual UX is verification URI + user
     code + background poll, with PKCE as a security extension).
  2. New MiniMax branch added to ``_start_device_code_flow``.
  3. Dispatcher tightened: pkce branch now requires
     ``provider_id == "anthropic"``, so any future PKCE provider added
     without an explicit branch gets a clean ``400 Unsupported flow``
     instead of silently launching Anthropic OAuth.

These tests pin the corrected behavior.
"""
import time
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from janus_cli.web_server import _SESSION_TOKEN, app

client = TestClient(app)
HEADERS = {"X-Janus-Session-Token": _SESSION_TOKEN}


def test_minimax_login_does_not_launch_anthropic_flow():
    """Click 'Login' on MiniMax → MUST NOT return claude.ai auth_url."""
    fake_user_code_resp = {
        "user_code": "ABCD-1234",
        "verification_uri": "https://api.minimax.io/oauth/verify",
        # `expired_in` < 1e12 so the heuristic treats it as seconds.
        "expired_in": 600,
        "interval": 2000,
        "state": "stub-state",
    }
    with patch(
        "janus_cli.auth._minimax_request_user_code",
        return_value=fake_user_code_resp,
    ), patch(
        "janus_cli.auth._minimax_pkce_pair",
        return_value=("verifier-stub", "challenge-stub", "stub-state"),
    ), patch(
        "janus_cli.web_server._minimax_poller",
        return_value=None,
    ):
        resp = client.post(
            "/api/providers/oauth/minimax-oauth/start",
            headers=HEADERS,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The bug used to return Anthropic's auth_url — make sure the response
    # references neither the auth_url field nor anything Claude-related.
    assert "auth_url" not in body
    assert "claude.ai" not in str(body).lower()

    # And the response IS the device-code shape pointing at MiniMax.
    assert body["flow"] == "device_code"
    assert "minimax" in body["verification_url"].lower()
    assert body["user_code"] == "ABCD-1234"
    assert body["expires_in"] == 600


def test_codex_dashboard_worker_persists_runtime_provider(tmp_path, monkeypatch):
    from janus_cli import web_server as ws
    from janus_cli.auth import get_active_provider
    from janus_cli.runtime_provider import resolve_runtime_provider

    access_token = "h.eyJleHAiOjk5OTk5OTk5OTl9.s"

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/deviceauth/usercode"):
                return _Resp(200, {
                    "device_auth_id": "device-auth-id",
                    "interval": 3,
                    "user_code": "CODEX-1234",
                })
            if url.endswith("/deviceauth/token"):
                return _Resp(200, {
                    "authorization_code": "authorization-code",
                    "code_verifier": "code-verifier",
                })
            return _Resp(200, {
                "access_token": access_token,
                "refresh_token": "codex-refresh",
            })

    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(ws.time, "sleep", lambda _: None)

    sid, _ = ws._new_oauth_session("openai-codex", "device_code")
    try:
        ws._codex_full_login_worker(sid)

        assert ws._oauth_sessions[sid]["status"] == "approved"
        assert get_active_provider() == "openai-codex"

        runtime = resolve_runtime_provider(requested=None)
        assert runtime["provider"] == "openai-codex"
        assert runtime["api_key"] == access_token
        assert runtime["api_mode"] == "codex_responses"
    finally:
        ws._oauth_sessions.pop(sid, None)


def test_minimax_dashboard_poller_accepts_absolute_ms_expired_in():
    """Dashboard MiniMax completion must accept unix-ms token expiry values."""
    from janus_cli import web_server as ws

    now = datetime.now(timezone.utc)
    abs_ms = int((now.timestamp() + 1800) * 1000)
    session_id = "minimax-absolute-ms-test"
    ws._oauth_sessions[session_id] = {
        "session_id": session_id,
        "provider": "minimax-oauth",
        "flow": "device_code",
        "created_at": time.time(),
        "status": "pending",
        "error_message": None,
        "portal_base_url": "https://api.minimax.io",
        "client_id": "client-id",
        "user_code": "ABCD-1234",
        "code_verifier": "verifier",
        "interval_ms": 2000,
        "expired_in_raw": abs_ms,
        "region": "global",
    }
    captured_state = {}

    try:
        with patch(
            "janus_cli.auth._minimax_poll_token",
            return_value={
                "status": "success",
                "access_token": "access",
                "refresh_token": "refresh",
                "expired_in": abs_ms,
                "token_type": "Bearer",
            },
        ), patch(
            "janus_cli.auth._minimax_save_auth_state",
            side_effect=lambda state: captured_state.update(state),
        ):
            ws._minimax_poller(session_id)
    finally:
        ws._oauth_sessions.pop(session_id, None)

    assert captured_state["access_token"] == "access"
    assert 1790 <= captured_state["expires_in"] <= 1810
    assert datetime.fromisoformat(captured_state["expires_at"]).year < 9999


def test_anthropic_pkce_branch_still_works():
    """Sanity: the dispatcher tightening doesn't break the legitimate Anthropic PKCE path."""
    fake_anthropic_response = {
        "session_id": "stub-session",
        "flow": "pkce",
        "auth_url": "https://claude.ai/oauth/authorize?code=true&...",
        "expires_in": 600,
    }
    with patch(
        "janus_cli.web_server._start_anthropic_pkce",
        return_value=fake_anthropic_response,
    ):
        resp = client.post(
            "/api/providers/oauth/anthropic/start",
            headers=HEADERS,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["flow"] == "pkce"
    assert "claude.ai" in body["auth_url"]


_XAI_DEVICE = {
    "device_code": "xai-dev",
    "user_code": "GROK-1234",
    "verification_uri": "https://accounts.x.ai/oauth2/device",
    "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=GROK-1234",
    "expires_in": 1800,
    "interval": 5,
}


def test_xai_oauth_listed_as_device_code_flow():
    """xAI Grok OAuth uses the device-code flow: no 127.0.0.1 callback listener."""
    resp = client.get("/api/providers/oauth", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    providers = {p["id"]: p for p in resp.json()["providers"]}
    assert providers["xai-oauth"]["flow"] == "device_code"
    assert "grok" in providers["xai-oauth"]["name"].lower()


def test_xai_device_start_returns_code_and_prefilled_url(monkeypatch):
    from janus_cli import web_server as ws
    from janus_cli import xai_device_login as dl

    monkeypatch.setattr(dl, "request_xai_device_code", lambda client, **k: dict(_XAI_DEVICE))
    monkeypatch.setattr(ws, "_xai_device_poller", lambda sid: None)

    resp = client.post("/api/providers/oauth/xai-oauth/start", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    try:
        assert body["flow"] == "device_code"
        assert body["user_code"] == "GROK-1234"
        assert body["verification_url"] == _XAI_DEVICE["verification_uri_complete"]
        assert body["poll_interval"] == 5
        sess = ws._oauth_sessions[body["session_id"]]
        assert sess["provider"] == "xai-oauth"
        assert sess["flow"] == "device_code"
        assert "server" not in sess  # nothing bound on 127.0.0.1
    finally:
        ws._oauth_sessions.pop(body["session_id"], None)


def _xai_device_session(ws, session_id):
    ws._oauth_sessions[session_id] = {
        "session_id": session_id,
        "provider": "xai-oauth",
        "flow": "device_code",
        "created_at": time.time(),
        "status": "pending",
        "error_message": None,
        "device_code": "xai-dev",
        "interval": 5,
        "expires_at": time.time() + 1800,
    }


def _stub_xai_discovery(monkeypatch):
    from janus_cli import auth as auth_mod

    monkeypatch.setattr(
        auth_mod,
        "_xai_oauth_discovery",
        lambda *a, **k: {
            "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        },
    )


def test_xai_device_poller_persists_tokens_on_approval(monkeypatch):
    from janus_cli import auth as auth_mod
    from janus_cli import web_server as ws
    from janus_cli import xai_device_login as dl

    saved = {}
    session_id = "xai-device-success-test"
    _xai_device_session(ws, session_id)
    _stub_xai_discovery(monkeypatch)
    monkeypatch.setattr(
        dl,
        "poll_xai_device_token",
        lambda client, **k: {"access_token": "xai-access", "refresh_token": "xai-refresh", "expires_in": 3600},
    )
    monkeypatch.setattr(auth_mod, "_save_xai_oauth_tokens", lambda tokens, **k: saved.update(tokens))
    monkeypatch.setattr(ws, "_drop_stale_dashboard_xai_pool_entries", lambda: None)

    try:
        ws._xai_device_poller(session_id)
        assert ws._oauth_sessions[session_id]["status"] == "approved"
        assert saved["access_token"] == "xai-access"
        assert saved["refresh_token"] == "xai-refresh"
    finally:
        ws._oauth_sessions.pop(session_id, None)


def test_xai_device_poller_skips_persist_when_cancelled(monkeypatch):
    from janus_cli import auth as auth_mod
    from janus_cli import web_server as ws
    from janus_cli import xai_device_login as dl

    saved = {}
    session_id = "xai-device-cancel-test"
    _xai_device_session(ws, session_id)
    _stub_xai_discovery(monkeypatch)

    def _poll(client, **k):
        # The user cancels while xAI is still waiting for approval.
        ws._oauth_sessions.pop(session_id, None)
        return {"access_token": "xai-access", "refresh_token": "xai-refresh"}

    monkeypatch.setattr(dl, "poll_xai_device_token", _poll)
    monkeypatch.setattr(auth_mod, "_save_xai_oauth_tokens", lambda tokens, **k: saved.update(tokens))

    ws._xai_device_poller(session_id)
    assert saved == {}


def test_xai_device_poller_passes_cancel_check(monkeypatch):
    """A DELETE on the session must stop the poll loop, not wait out the code."""
    from janus_cli import web_server as ws
    from janus_cli import xai_device_login as dl

    session_id = "xai-device-cancel-check-test"
    _xai_device_session(ws, session_id)
    _stub_xai_discovery(monkeypatch)
    seen = {}

    def _poll(client, **k):
        seen["before"] = k["should_cancel"]()
        ws._oauth_sessions.pop(session_id, None)
        seen["after"] = k["should_cancel"]()
        raise dl.AuthError("cancelled", provider="xai-oauth", code="xai_device_login_cancelled")

    monkeypatch.setattr(dl, "poll_xai_device_token", _poll)
    ws._xai_device_poller(session_id)
    assert seen == {"before": False, "after": True}


def test_xai_device_poller_reports_errors(monkeypatch):
    from janus_cli import web_server as ws
    from janus_cli import xai_device_login as dl

    session_id = "xai-device-error-test"
    _xai_device_session(ws, session_id)
    _stub_xai_discovery(monkeypatch)

    def _poll(client, **k):
        raise dl.AuthError("xAI sign-in was declined.", provider="xai-oauth", code="xai_device_access_denied")

    monkeypatch.setattr(dl, "poll_xai_device_token", _poll)
    try:
        ws._xai_device_poller(session_id)
        sess = ws._oauth_sessions[session_id]
        assert sess["status"] == "error"
        assert "declined" in sess["error_message"]
    finally:
        ws._oauth_sessions.pop(session_id, None)


def test_dashboard_login_drops_stale_duplicate_pool_entries(monkeypatch):
    """Old dashboard logins added a pool entry sharing the singleton's single-use
    refresh token; after one side refreshes, the other is dead. A new login
    removes them and does not add another."""
    from janus_cli import web_server as ws

    class _Entry:
        def __init__(self, id_, source):
            self.id = id_
            self.source = source

    class _Pool:
        def __init__(self):
            self.items = [_Entry("a", "manual:dashboard_xai_pkce"), _Entry("b", "loopback_pkce")]

        def entries(self):
            return list(self.items)

        def remove_entry(self, id_):
            self.items = [e for e in self.items if e.id != id_]

    pool = _Pool()
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)
    ws._drop_stale_dashboard_xai_pool_entries()
    assert [e.source for e in pool.items] == ["loopback_pkce"]


def test_unknown_pkce_provider_rejected_cleanly():
    """A future PKCE provider without an explicit branch must NOT silently route to Anthropic.

    Simulates a hypothetical catalog entry with ``flow: "pkce"`` and an
    id other than "anthropic". The dispatcher should fall through past
    the pkce branch (now gated on provider_id) and the device_code
    branch, then hit "Unsupported flow" — proving the bug class is
    structurally prevented.
    """
    from janus_cli import web_server as ws

    # Inject a hypothetical catalog entry that's pkce-flagged but isn't
    # anthropic. This shape mirrors what would happen if a developer
    # added a new provider entry without remembering to wire up its
    # start function.
    fake_entry = {
        "id": "hypothetical-pkce-provider",
        "name": "Hypothetical PKCE Provider",
        "flow": "pkce",
        "cli_command": "janus auth add hypothetical-pkce-provider",
        "docs_url": "https://example.com",
        "status_fn": None,
    }
    original_catalog = ws._OAUTH_PROVIDER_CATALOG
    try:
        ws._OAUTH_PROVIDER_CATALOG = original_catalog + (fake_entry,)
        resp = client.post(
            "/api/providers/oauth/hypothetical-pkce-provider/start",
            headers=HEADERS,
        )
    finally:
        ws._OAUTH_PROVIDER_CATALOG = original_catalog

    # Either 400 "Unsupported flow" (the explicit fall-through) or any
    # 4xx — what we MUST NOT see is a 200 with claude.ai in the body.
    assert resp.status_code >= 400, resp.text
    assert "claude.ai" not in resp.text.lower()
