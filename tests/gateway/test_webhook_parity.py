"""Webhook parity features: Standard Webhooks signatures, cron_job routes, coalescing."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH

_SECRET = "whsec_" + base64.b64encode(b"standard-webhooks-test-key").decode()


def _adapter(routes):
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": routes, "rate_limit": 100},
        )
    )


def _app(adapter):
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _request(headers):
    req = MagicMock()
    req.headers = headers
    return req


def _standard_headers(body: bytes, *, msg_id="msg_2b", timestamp=None, secret=_SECRET):
    timestamp = str(int(time.time())) if timestamp is None else timestamp
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{sig}",
    }


# ── Standard Webhooks ───────────────────────────────────────────────────


class TestStandardWebhooks:
    def test_valid_signature_is_accepted(self):
        body = b'{"type":"invoice.paid"}'
        adapter = _adapter({})

        assert adapter._validate_signature(_request(_standard_headers(body)), body, _SECRET) is True

    def test_signature_is_bound_to_the_body(self):
        body = b'{"type":"invoice.paid"}'
        headers = _standard_headers(body)
        adapter = _adapter({})

        assert adapter._validate_signature(_request(headers), b'{"type":"forged"}', _SECRET) is False

    def test_stale_timestamp_is_rejected(self):
        body = b"{}"
        headers = _standard_headers(body, timestamp=str(int(time.time()) - 3600))
        adapter = _adapter({})

        assert adapter._validate_signature(_request(headers), body, _SECRET) is False

    def test_any_rotated_signature_may_match(self):
        body = b"{}"
        headers = _standard_headers(body)
        headers["webhook-signature"] = "v1,bm90LXRoaXMtb25l " + headers["webhook-signature"]
        adapter = _adapter({})

        assert adapter._validate_signature(_request(headers), body, _SECRET) is True

    @pytest.mark.asyncio
    async def test_webhook_id_deduplicates_retries(self):
        adapter = _adapter({"billing": {"secret": _SECRET, "prompt": "{type}"}})
        adapter.handle_message = AsyncMock()
        body = json.dumps({"type": "invoice.paid"}).encode()
        headers = {**_standard_headers(body, msg_id="msg_retry"), "Content-Type": "application/json"}

        async with TestClient(TestServer(_app(adapter))) as cli:
            first = await cli.post("/webhooks/billing", data=body, headers=headers)
            second = await cli.post("/webhooks/billing", data=body, headers=headers)
            assert first.status == 202
            assert (await second.json())["status"] == "duplicate"


# ── cron_job routes ──────────────────────────────────────────────────────


@pytest.fixture
def cron_jobs(monkeypatch):
    jobs = {
        "nightly": {"id": "abc123", "name": "nightly", "enabled": True, "state": "scheduled"},
        "held": {"id": "def456", "name": "held", "enabled": False, "state": "paused"},
    }
    triggered = []

    import cron.jobs as cron_jobs_mod

    monkeypatch.setattr(cron_jobs_mod, "resolve_job_ref", lambda ref: jobs.get(ref))
    monkeypatch.setattr(
        cron_jobs_mod, "trigger_job", lambda ref: triggered.append(ref) or jobs.get(ref)
    )
    return triggered


class TestCronJobRoute:
    @pytest.mark.asyncio
    async def test_event_triggers_the_job_instead_of_the_agent(self, cron_jobs):
        adapter = _adapter({"deploy": {"secret": _INSECURE_NO_AUTH, "cron_job": "nightly"}})
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post("/webhooks/deploy", json={"type": "deploy.done"})
            data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "triggered"
        assert data["job_id"] == "abc123"
        assert cron_jobs == ["abc123"]
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_job_is_404_and_triggers_nothing(self, cron_jobs):
        adapter = _adapter({"deploy": {"secret": _INSECURE_NO_AUTH, "cron_job": "missing"}})

        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post("/webhooks/deploy", json={})

        assert resp.status == 404
        assert cron_jobs == []

    @pytest.mark.asyncio
    async def test_paused_job_is_not_resumed_by_an_event(self, cron_jobs):
        adapter = _adapter({"deploy": {"secret": _INSECURE_NO_AUTH, "cron_job": "held"}})

        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await cli.post("/webhooks/deploy", json={})

        assert resp.status == 409
        assert cron_jobs == []


# ── Coalescing ───────────────────────────────────────────────────────────


async def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)


class TestCoalesce:
    @pytest.mark.asyncio
    async def test_burst_becomes_one_agent_run_with_every_event(self):
        adapter = _adapter(
            {"alerts": {"secret": _INSECURE_NO_AUTH, "prompt": "alert {name}", "coalesce_seconds": 0.2}}
        )
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as cli:
            statuses = []
            for i in range(3):
                resp = await cli.post(
                    "/webhooks/alerts", json={"name": f"a{i}"}, headers={"X-Request-ID": f"r{i}"}
                )
                statuses.append((await resp.json())["status"])
            await _wait_for(lambda: adapter.handle_message.await_count >= 1)
            await asyncio.sleep(0.3)

        assert statuses == ["accepted", "coalesced", "coalesced"]
        assert adapter.handle_message.await_count == 1
        text = adapter.handle_message.await_args.args[0].text
        assert all(f"alert a{i}" in text for i in range(3))

    @pytest.mark.asyncio
    async def test_coalesce_key_keeps_separate_subjects_apart(self):
        adapter = _adapter(
            {
                "prs": {
                    "secret": _INSECURE_NO_AUTH,
                    "prompt": "PR {number}",
                    "coalesce_seconds": 0.2,
                    "coalesce_key": "{number}",
                }
            }
        )
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as cli:
            for i, number in enumerate([1, 2, 1]):
                await cli.post("/webhooks/prs", json={"number": number}, headers={"X-Request-ID": f"p{i}"})
            await _wait_for(lambda: adapter.handle_message.await_count >= 2)
            await asyncio.sleep(0.3)

        texts = sorted(call.args[0].text for call in adapter.handle_message.await_args_list)
        assert len(texts) == 2
        assert texts[0].count("PR 1") == 2
        assert "PR 2" in texts[1]

    @pytest.mark.asyncio
    async def test_single_event_keeps_its_plain_prompt(self):
        adapter = _adapter(
            {"alerts": {"secret": _INSECURE_NO_AUTH, "prompt": "alert {name}", "coalesce_seconds": 0.1}}
        )
        adapter.handle_message = AsyncMock()

        async with TestClient(TestServer(_app(adapter))) as cli:
            await cli.post("/webhooks/alerts", json={"name": "solo"})
            await _wait_for(lambda: adapter.handle_message.await_count >= 1)

        assert adapter.handle_message.await_args.args[0].text == "alert solo"

    @pytest.mark.asyncio
    async def test_cron_route_burst_triggers_once(self, cron_jobs):
        adapter = _adapter(
            {"deploy": {"secret": _INSECURE_NO_AUTH, "cron_job": "nightly", "coalesce_seconds": 0.2}}
        )

        async with TestClient(TestServer(_app(adapter))) as cli:
            for i in range(3):
                await cli.post("/webhooks/deploy", json={}, headers={"X-Request-ID": f"d{i}"})
            await _wait_for(lambda: cron_jobs)
            await asyncio.sleep(0.3)

        assert cron_jobs == ["abc123"]

    @pytest.mark.parametrize("value", [0, -1, "soon", None])
    def test_invalid_window_disables_coalescing(self, value):
        assert WebhookAdapter._coalesce_window({"coalesce_seconds": value}) == 0.0
