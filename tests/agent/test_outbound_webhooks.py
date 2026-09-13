"""HMAC-signed outbound lifecycle webhooks."""

import hashlib
import hmac
import json

from agent.outbound_webhooks import HOOK_EVENT_MAP, _json_safe, _sign, emit, emit_hook


def test_sign_is_hmac_sha256():
    body = b'{"event":"cron.complete"}'
    secret = "s3cret"
    assert _sign(secret, body) == hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_json_safe_strips_objects():
    class Boom:
        def __str__(self):
            return "boom"

    out = _json_safe({"ok": True, "obj": Boom(), "nested": {"n": 1}})
    assert out["ok"] is True
    assert out["obj"] == "boom"
    assert out["nested"]["n"] == 1


def test_emit_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JANUS_OUTBOUND_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(
        "agent.outbound_webhooks._load_outbound_config",
        lambda: {"enabled": False, "urls": ["https://example.invalid"]},
    )
    assert emit("cron.complete", {"job_id": "x"}) is False


def test_emit_hook_ignores_unmapped():
    assert emit_hook("pre_tool_call", tool_name="terminal") is False
    assert "on_session_finalize" in HOOK_EVENT_MAP


def test_emit_queues_signed_post(monkeypatch):
    posted = []

    def fake_thread(*, target, args, name, daemon):
        class _T:
            def start(self):
                target(*args)

        return _T()

    def fake_post(url, body, headers):
        posted.append((url, body, headers))

    monkeypatch.setattr("agent.outbound_webhooks.threading.Thread", fake_thread)
    monkeypatch.setattr("agent.outbound_webhooks._post", fake_post)
    monkeypatch.setattr(
        "agent.outbound_webhooks._load_outbound_config",
        lambda: {
            "enabled": True,
            "secret": "abc",
            "urls": ["https://hooks.example/janus"],
            "events": ["cron.complete"],
        },
    )
    assert emit("cron.complete", {"job_id": "deadbeef"}) is True
    assert len(posted) == 1
    url, body, headers = posted[0]
    assert url == "https://hooks.example/janus"
    assert headers["X-Janus-Event"] == "cron.complete"
    expected = "sha256=" + hmac.new(b"abc", body, hashlib.sha256).hexdigest()
    assert headers["X-Janus-Signature"] == expected
    payload = json.loads(body)
    assert payload["event"] == "cron.complete"
    assert payload["payload"]["job_id"] == "deadbeef"
