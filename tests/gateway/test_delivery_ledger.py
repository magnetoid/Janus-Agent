"""Policy and replay for the delivery-obligation ledger (plan item 2.2).

``DeliveryLedger`` wraps the ``delivery_obligations`` primitives in
``SessionDB`` with the decisions the gateway needs: how long to wait before
retrying a failed send, when to give up, and how to replay what is owed once
adapters are connected again. Recording must never break a live send, so the
ledger swallows its own errors.
"""
import asyncio

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.delivery_ledger import DeliveryLedger
from gateway.platforms.base import SendResult
from janus_state import SessionDB

T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class RecordingAdapter:
    def __init__(self, results=None):
        self.sent = []
        self._results = list(results or [])

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content,
                          "reply_to": reply_to, "metadata": metadata})
        if self._results:
            res = self._results.pop(0)
            if isinstance(res, Exception):
                raise res
            return res
        return SendResult(success=True, message_id=f"m{len(self.sent)}")


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def ledger(db, clock):
    return DeliveryLedger(db, clock=clock)


def _record(ledger, **overrides):
    kwargs = dict(kind="reply", platform="telegram", chat_id="42", content="hi",
                  thread_id="7", reply_to="r1", metadata={"thread_id": "7"},
                  session_id="s1")
    kwargs.update(overrides)
    return ledger.record(**kwargs)


def _run(coro):
    return asyncio.run(coro)


# --- record / settle -------------------------------------------------------

def test_record_then_successful_settle_is_delivered(ledger, db):
    oid = _record(ledger)
    assert db.get_obligation(oid)["status"] == "sending"
    ledger.settle(oid, SendResult(success=True, message_id="m-9"))
    row = db.get_obligation(oid)
    assert row["status"] == "delivered"
    assert row["message_id"] == "m-9"


def test_failed_settles_walk_the_backoff_schedule_then_abandon(ledger, db, clock):
    oid = _record(ledger)
    expected_delays = list(dl.BACKOFF_SCHEDULE_S)
    for attempt in range(1, dl.MAX_ATTEMPTS + 1):
        ledger.settle(oid, SendResult(success=False, error="net down"))
        row = db.get_obligation(oid)
        assert row["attempts"] == attempt
        if attempt < dl.MAX_ATTEMPTS:
            delay = expected_delays[min(attempt - 1, len(expected_delays) - 1)]
            assert row["status"] == "pending"
            assert row["next_attempt_at"] == clock.t + delay
            assert row["last_error"] == "net down"
        else:
            assert row["status"] == "abandoned"


def test_settle_with_exception_counts_as_a_failed_attempt(ledger, db):
    oid = _record(ledger)
    ledger.settle(oid, error=RuntimeError("socket closed"))
    row = db.get_obligation(oid)
    assert row["status"] == "pending"
    assert "socket closed" in row["last_error"]


def test_ledger_never_raises_when_storage_is_broken(clock):
    class BrokenDB:
        def __getattr__(self, name):
            raise RuntimeError("disk full")

    ledger = DeliveryLedger(BrokenDB(), clock=clock)
    assert _record(ledger) is None
    ledger.settle(None, SendResult(success=True))          # no-op, no raise
    ledger.settle("missing", SendResult(success=False, error="x"))
    assert _run(ledger.replay_once({})) == {"delivered": 0, "failed": 0, "skipped": 0, "abandoned": 0}


def test_disabled_ledger_records_nothing(db, clock):
    ledger = DeliveryLedger(db, clock=clock, enabled=False)
    assert _record(ledger) is None
    assert db.list_obligations() == []


def test_from_config_honours_the_gateway_switch(db):
    assert DeliveryLedger.from_config(db, {"gateway": {"delivery_ledger": False}}) is None
    assert isinstance(DeliveryLedger.from_config(db, {"gateway": {"delivery_ledger": True}}), DeliveryLedger)
    assert isinstance(DeliveryLedger.from_config(db, {}), DeliveryLedger)   # default on
    assert DeliveryLedger.from_config(None, {}) is None                     # no state.db


# --- replay ----------------------------------------------------------------

def _pending(ledger, db, clock, **overrides):
    """A row whose live send failed and is now due."""
    oid = _record(ledger, **overrides)
    ledger.settle(oid, SendResult(success=False, error="net"))
    clock.t += dl.BACKOFF_SCHEDULE_S[0] + 1
    return oid


def test_replay_sends_due_rows_through_the_platform_adapter_once(ledger, db, clock):
    oid = _pending(ledger, db, clock)
    adapter = RecordingAdapter()
    counts = _run(ledger.replay_once({Platform.TELEGRAM: adapter}))
    assert counts["delivered"] == 1
    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert sent["chat_id"] == "42" and sent["content"] == "hi" and sent["reply_to"] == "r1"
    assert sent["metadata"]["thread_id"] == "7"
    assert sent["metadata"]["notify"] is True
    assert sent["metadata"]["replayed"] is True
    row = db.get_obligation(oid)
    assert row["status"] == "delivered" and row["message_id"] == "m1"
    # idempotent: nothing left to send
    assert _run(ledger.replay_once({Platform.TELEGRAM: adapter}))["delivered"] == 0
    assert len(adapter.sent) == 1


def test_replay_leaves_rows_alone_when_their_platform_is_not_connected(ledger, db, clock):
    oid = _pending(ledger, db, clock)
    counts = _run(ledger.replay_once({Platform.DISCORD: RecordingAdapter()}))
    assert counts == {"delivered": 0, "failed": 0, "skipped": 1, "abandoned": 0}
    row = db.get_obligation(oid)
    assert row["status"] == "pending"
    assert row["attempts"] == 1                  # not charged for our absence
    assert row["next_attempt_at"] > clock.t      # but not hammered either


def test_replay_failure_requeues_with_backoff(ledger, db, clock):
    oid = _pending(ledger, db, clock)
    adapter = RecordingAdapter(results=[SendResult(success=False, error="429")])
    counts = _run(ledger.replay_once({Platform.TELEGRAM: adapter}))
    assert counts["failed"] == 1
    row = db.get_obligation(oid)
    assert row["status"] == "pending" and row["attempts"] == 2
    assert row["next_attempt_at"] == clock.t + dl.BACKOFF_SCHEDULE_S[1]
    assert row["last_error"] == "429"


def test_replay_adapter_exception_is_a_failed_attempt_not_a_crash(ledger, db, clock):
    _pending(ledger, db, clock)
    adapter = RecordingAdapter(results=[ConnectionError("reset")])
    counts = _run(ledger.replay_once({Platform.TELEGRAM: adapter}))
    assert counts["failed"] == 1
    assert "reset" in db.list_obligations(status="pending")[0]["last_error"]


def test_replay_abandons_rows_older_than_max_age_without_sending(ledger, db, clock):
    oid = _pending(ledger, db, clock)
    clock.t += dl.MAX_AGE_S + 1
    adapter = RecordingAdapter()
    counts = _run(ledger.replay_once({Platform.TELEGRAM: adapter}))
    assert counts["abandoned"] == 1 and adapter.sent == []
    row = db.get_obligation(oid)
    assert row["status"] == "abandoned" and "expired" in row["last_error"]


def test_replay_accepts_an_injected_sender_for_standalone_use(ledger, db, clock):
    _pending(ledger, db, clock, platform="slack", chat_id="C1")
    calls = []

    async def send(obligation):
        calls.append(obligation)
        return SendResult(success=True, message_id="ts-1")

    counts = _run(ledger.replay_once(send=send))
    assert counts["delivered"] == 1
    assert calls[0]["platform"] == "slack" and calls[0]["chat_id"] == "C1"


def test_replay_reclaims_a_send_left_in_flight_by_a_dead_process(ledger, db, clock):
    # Recorded, never settled: the previous gateway died mid-send.
    oid = _record(ledger)
    clock.t += dl.STALE_SENDING_S + 1
    adapter = RecordingAdapter()
    counts = _run(ledger.replay_once({Platform.TELEGRAM: adapter}))
    assert counts["delivered"] == 1
    assert db.get_obligation(oid)["status"] == "delivered"


def test_pending_summary_lists_what_is_owed(ledger, db, clock):
    _pending(ledger, db, clock, content="one")
    _record(ledger, content="in-flight")
    owed = ledger.pending()
    assert [o["content"] for o in owed] == ["one"]
