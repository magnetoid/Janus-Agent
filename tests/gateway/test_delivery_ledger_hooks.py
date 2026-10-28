"""Where the delivery ledger plugs into the live send paths (plan item 2.2).

Three call sites owe a reply to a platform: the adapter's final-reply send
(``BasePlatformAdapter._process_message_background``), cron result delivery
(``cron.scheduler._deliver_result``), and the gateway's replay watcher that
drains what the first two could not deliver.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.delivery_ledger import DeliveryLedger
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource, build_session_key
from janus_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def ledger(db):
    return DeliveryLedger(db)


# ---------------------------------------------------------------------------
# Adapter final-reply send
# ---------------------------------------------------------------------------

class StubAdapter(BasePlatformAdapter):
    def __init__(self, db, results=None):
        super().__init__(PlatformConfig(enabled=True, token="fake"), Platform.DISCORD)
        self._db = db
        self._results = list(results or [])
        self.sent = []
        self.status_at_send = []

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        rows = self._db.list_obligations()
        self.status_at_send.append(rows[0]["status"] if rows else None)
        if self._results:
            res = self._results.pop(0)
            if isinstance(res, Exception):
                raise res
            return res
        return SendResult(success=True, message_id="msg1")

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _event(text="hello", chat_id="c1", thread_id=None):
    return MessageEvent(
        text=text,
        source=SessionSource(platform=Platform.DISCORD, chat_id=chat_id,
                             chat_type="dm", user_id="u1", thread_id=thread_id),
        message_id="m1",
    )


async def _reply(adapter, text="the reply", thread_id=None):
    async def handler(event):
        return text

    adapter.set_message_handler(handler)
    event = _event(thread_id=thread_id)
    await adapter._process_message_background(event, build_session_key(event.source))
    await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_final_reply_is_recorded_before_send_and_delivered_after(db, ledger):
    adapter = StubAdapter(db)
    adapter.set_delivery_ledger(ledger)
    await _reply(adapter, thread_id="t9")

    assert adapter.status_at_send == ["sending"]          # row existed before the send
    rows = db.list_obligations()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "reply"
    assert row["platform"] == "discord"
    assert row["chat_id"] == "c1"
    assert row["thread_id"] == "t9"
    assert row["content"] == "the reply"
    assert row["status"] == "delivered"
    assert row["message_id"] == "msg1"
    assert row["metadata"]["notify"] is True


@pytest.mark.asyncio
async def test_failed_final_reply_stays_owed(db, ledger):
    adapter = StubAdapter(db, results=[SendResult(success=False, error="platform down")] * 4)
    adapter.set_delivery_ledger(ledger)
    await _reply(adapter)

    rows = db.list_obligations()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["attempts"] == 1
    assert rows[0]["last_error"] == "platform down"


@pytest.mark.asyncio
async def test_send_exception_is_recorded_not_lost(db, ledger):
    adapter = StubAdapter(db, results=[ConnectionError("reset")] * 4)
    adapter.set_delivery_ledger(ledger)
    await _reply(adapter)

    rows = db.list_obligations()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert "reset" in rows[0]["last_error"]


@pytest.mark.asyncio
async def test_adapter_without_ledger_sends_normally(db):
    adapter = StubAdapter(db)
    await _reply(adapter)
    assert [s["content"] for s in adapter.sent] == ["the reply"]
    assert db.list_obligations() == []


# ---------------------------------------------------------------------------
# Cron result delivery
# ---------------------------------------------------------------------------

def _cron_env(monkeypatch, ledger, send_results):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_delivery_ledger", lambda: ledger)
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: SimpleNamespace(platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)}),
    )
    calls = []

    async def fake_send(platform, pconfig, chat_id, message, thread_id=None, media_files=None, **kw):
        calls.append({"platform": platform, "chat_id": chat_id, "message": message, "thread_id": thread_id})
        return send_results.pop(0) if send_results else {}

    monkeypatch.setattr("tools.send_message_tool._send_to_platform", fake_send)
    return scheduler, calls


def test_cron_delivery_records_an_obligation_and_settles_it(db, ledger, monkeypatch):
    scheduler, calls = _cron_env(monkeypatch, ledger, send_results=[{}])
    job = {"id": "job-1", "name": "digest", "deliver": "telegram:42:7"}

    assert scheduler._deliver_result(job, "report body") is None
    assert len(calls) == 1
    rows = db.list_obligations()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "cron"
    assert row["source_ref"] == "job-1"
    assert row["platform"] == "telegram"
    assert row["chat_id"] == "42"
    assert row["thread_id"] == "7"
    assert row["content"] == calls[0]["message"]      # the exact text that went out
    assert row["status"] == "delivered"


def test_cron_delivery_failure_leaves_the_obligation_pending(db, ledger, monkeypatch):
    scheduler, _ = _cron_env(monkeypatch, ledger, send_results=[{"error": "rate limited"}])
    job = {"id": "job-2", "name": "digest", "deliver": "telegram:42"}

    err = scheduler._deliver_result(job, "report body")
    assert err and "rate limited" in err
    rows = db.list_obligations()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert "rate limited" in rows[0]["last_error"]


def test_cron_delivery_to_unconfigured_platform_records_nothing(db, ledger, monkeypatch):
    scheduler, _ = _cron_env(monkeypatch, ledger, send_results=[])
    job = {"id": "job-3", "name": "digest", "deliver": "discord:1"}
    assert scheduler._deliver_result(job, "report body")      # error string
    assert db.list_obligations() == []


# ---------------------------------------------------------------------------
# Gateway replay watcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_watcher_drains_the_ledger_through_connected_adapters(monkeypatch):
    from gateway.runner import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: object()}
    calls = []

    class FakeLedger:
        async def replay_once(self, adapters, **kwargs):
            calls.append(adapters)
            return {"delivered": 0, "failed": 0, "skipped": 0, "abandoned": 0}

    runner._delivery_ledger = FakeLedger()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            runner._running = False

    monkeypatch.setattr("gateway.runner.asyncio.sleep", fake_sleep)
    await runner._delivery_replay_watcher(interval=0)
    assert calls == [runner.adapters]


@pytest.mark.asyncio
async def test_replay_watcher_is_a_noop_without_a_ledger(monkeypatch):
    from gateway.runner import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner._delivery_ledger = None
    await runner._delivery_replay_watcher(interval=0)   # returns immediately
