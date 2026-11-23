"""xAI Grok OAuth device-code login (RFC 8628).

The loopback-redirect flow (browser → 127.0.0.1:56121/callback) kept
failing for users: the popup opened, but the redirect never reached the
listener (or arrived with no ``code``), and the CLI timed out. xAI's OIDC
discovery advertises ``device_authorization_endpoint`` and the device-code
grant, so the default login now shows a code and polls the token endpoint
instead — no local listener, no redirect.
"""

from __future__ import annotations

import argparse
from urllib.parse import parse_qs

import httpx
import pytest

from janus_cli import auth as auth_mod
from janus_cli import xai_device_login as dl
from janus_cli.auth import AuthError

DEVICE_PAYLOAD = {
    "device_code": "dev-123",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://accounts.x.ai/oauth2/device",
    "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH",
    "expires_in": 1800,
    "interval": 5,
}
TOKENS = {
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "id_token": "id-1",
    "expires_in": 3600,
    "token_type": "Bearer",
}
TOKEN_URL = "https://auth.x.ai/oauth2/token"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(responses):
    """httpx client that replays ``responses`` (callables or Response) in order."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        return item(request) if callable(item) else item

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.seen = seen  # type: ignore[attr-defined]
    return client


def _poll(client, clock, **kw):
    return dl.poll_xai_device_token(
        client,
        token_endpoint=TOKEN_URL,
        device_code="dev-123",
        expires_in=kw.pop("expires_in", 1800),
        interval=kw.pop("interval", 5),
        sleep=clock.sleep,
        clock=clock.clock,
        **kw,
    )


# ── device-code request ────────────────────────────────────────────────────


def test_request_posts_client_id_and_scope_without_loopback_params():
    client = _client([httpx.Response(200, json=DEVICE_PAYLOAD)])
    data = dl.request_xai_device_code(client)
    assert data["user_code"] == "ABCD-EFGH"
    req = client.seen[0]
    assert str(req.url) == "https://auth.x.ai/oauth2/device/code"
    form = parse_qs(req.content.decode())
    assert form["client_id"] == [auth_mod.XAI_OAUTH_CLIENT_ID]
    assert form["scope"] == [auth_mod.XAI_OAUTH_SCOPE]
    assert "redirect_uri" not in form


def test_request_non_200_raises_auth_error():
    client = _client([httpx.Response(403, text="blocked")])
    with pytest.raises(AuthError) as exc:
        dl.request_xai_device_code(client)
    assert exc.value.code == "xai_device_code_request_failed"


def test_request_missing_fields_raises_auth_error():
    partial = {k: v for k, v in DEVICE_PAYLOAD.items() if k != "user_code"}
    client = _client([httpx.Response(200, json=partial)])
    with pytest.raises(AuthError) as exc:
        dl.request_xai_device_code(client)
    assert exc.value.code == "xai_device_code_invalid"


# ── token polling ──────────────────────────────────────────────────────────


def test_poll_waits_through_pending_then_returns_tokens():
    clock = FakeClock()
    pending = httpx.Response(400, json={"error": "authorization_pending"})
    client = _client([pending, pending, httpx.Response(200, json=TOKENS)])
    payload = _poll(client, clock)
    assert payload["access_token"] == "at-1"
    assert len(client.seen) == 3
    assert clock.sleeps == [5, 5, 5]
    form = parse_qs(client.seen[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
    assert form["device_code"] == ["dev-123"]


def test_poll_slow_down_adds_five_seconds():
    clock = FakeClock()
    client = _client([
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json=TOKENS),
    ])
    _poll(client, clock)
    assert clock.sleeps == [5, 10]


@pytest.mark.parametrize(
    ("error", "code"),
    [("access_denied", "xai_device_access_denied"), ("expired_token", "xai_device_code_expired")],
)
def test_poll_terminal_errors_raise(error, code):
    clock = FakeClock()
    client = _client([httpx.Response(400, json={"error": error})])
    with pytest.raises(AuthError) as exc:
        _poll(client, clock)
    assert exc.value.code == code


def test_poll_times_out_when_code_expires():
    clock = FakeClock()
    pending = httpx.Response(400, json={"error": "authorization_pending"})
    client = _client([pending] * 10)
    with pytest.raises(AuthError) as exc:
        _poll(client, clock, expires_in=12, interval=5)
    assert exc.value.code == "xai_device_code_timeout"


def test_poll_keeps_going_through_edge_non_json_errors():
    """Cloudflare in front of auth.x.ai can answer a poll with an HTML 403."""
    clock = FakeClock()
    client = _client([
        httpx.Response(403, text="<html>cf</html>"),
        httpx.Response(200, json=TOKENS),
    ])
    assert _poll(client, clock)["refresh_token"] == "rt-1"


def test_poll_gives_up_after_repeated_edge_errors():
    clock = FakeClock()
    client = _client([httpx.Response(403, text="<html>cf</html>")] * 50)
    with pytest.raises(AuthError) as exc:
        _poll(client, clock)
    assert exc.value.code == "xai_device_token_failed"


def test_poll_stops_when_cancelled():
    clock = FakeClock()
    pending = httpx.Response(400, json={"error": "authorization_pending"})
    client = _client([pending] * 5)
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        # 1: before the first sleep, 2: after it, 3: before the second sleep
        return calls["n"] > 2

    with pytest.raises(AuthError) as exc:
        _poll(client, clock, should_cancel=cancelled)
    assert exc.value.code == "xai_device_login_cancelled"
    assert len(client.seen) == 1


def test_poll_rejects_success_without_refresh_token():
    clock = FakeClock()
    client = _client([httpx.Response(200, json={"access_token": "at-1"})])
    with pytest.raises(AuthError) as exc:
        _poll(client, clock)
    assert exc.value.code == "xai_device_token_invalid"


# ── full CLI login ─────────────────────────────────────────────────────────


@pytest.fixture
def discovery(monkeypatch):
    monkeypatch.setattr(
        auth_mod,
        "_xai_oauth_discovery",
        lambda *a, **k: {"authorization_endpoint": "https://auth.x.ai/oauth2/authorize", "token_endpoint": TOKEN_URL},
    )


def test_login_shows_code_opens_prefilled_url_and_returns_creds(monkeypatch, capsys, discovery):
    opened: list[str] = []
    monkeypatch.setattr(dl.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(auth_mod, "_is_remote_session", lambda: False)
    monkeypatch.setattr(auth_mod, "_can_open_graphical_browser", lambda: True)
    client = _client([httpx.Response(200, json=DEVICE_PAYLOAD), httpx.Response(200, json=TOKENS)])
    clock = FakeClock()

    creds = dl.xai_device_code_login(client=client, sleep=clock.sleep, clock=clock.clock)

    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out
    assert opened == [DEVICE_PAYLOAD["verification_uri_complete"]]
    assert creds["tokens"]["access_token"] == "at-1"
    assert creds["tokens"]["refresh_token"] == "rt-1"
    assert creds["source"] == "oauth-device-code"
    assert creds["redirect_uri"] == ""
    assert creds["discovery"]["token_endpoint"] == TOKEN_URL


def test_login_without_browser_only_prints(monkeypatch, capsys, discovery):
    opened: list[str] = []
    monkeypatch.setattr(dl.webbrowser, "open", lambda url: opened.append(url) or True)
    client = _client([httpx.Response(200, json=DEVICE_PAYLOAD), httpx.Response(200, json=TOKENS)])
    clock = FakeClock()
    dl.xai_device_code_login(open_browser=False, client=client, sleep=clock.sleep, clock=clock.clock)
    assert opened == []
    assert DEVICE_PAYLOAD["verification_uri"] in capsys.readouterr().out


# ── routing: device code is the default, loopback is opt-in ───────────────


def _route_login(monkeypatch, **flags):
    used: list[str] = []
    fake = {"tokens": dict(TOKENS), "discovery": {}, "redirect_uri": "", "base_url": "https://api.x.ai/v1"}
    monkeypatch.setattr(dl, "xai_device_code_login", lambda **k: used.append("device") or fake)
    monkeypatch.setattr(auth_mod, "_xai_oauth_loopback_login", lambda **k: used.append("loopback") or fake)
    monkeypatch.setattr(auth_mod, "_save_xai_oauth_tokens", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_update_config_for_provider", lambda *a, **k: "config.yaml")
    args = argparse.Namespace(
        timeout=None, no_browser=True, manual_paste=flags.get("manual_paste", False),
        loopback=flags.get("loopback", False),
    )
    auth_mod._login_xai_oauth(args, None, force_new_login=True)
    return used


def test_login_xai_oauth_defaults_to_device_code(monkeypatch):
    assert _route_login(monkeypatch) == ["device"]


def test_login_xai_oauth_loopback_flag_keeps_old_flow(monkeypatch):
    assert _route_login(monkeypatch, loopback=True) == ["loopback"]


def test_login_xai_oauth_manual_paste_keeps_old_flow(monkeypatch):
    assert _route_login(monkeypatch, manual_paste=True) == ["loopback"]
