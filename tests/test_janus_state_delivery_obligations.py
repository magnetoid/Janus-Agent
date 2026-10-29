"""Storage primitives for the delivery-obligation ledger (plan item 2.2).

An obligation is a reply the gateway owes a platform. It is written *before*
the send so a crash or a provider stall between "agent finished" and "message
acked" leaves a durable record that the replay watcher can act on. These
tests cover only the SQLite primitives; backoff/abandon policy lives in
``gateway.delivery_ledger`` and is tested there.
"""
import threading

import pytest

from janus_state import SessionDB

T0 = 1_800_000_000.0


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _create(db, **overrides):
    kwargs = dict(
        kind="reply", platform="telegram", chat_id="42", content="hello",
        thread_id="7", reply_to="99", metadata={"notify": True},
        session_id="sess-1", source_ref=None, now=T0,
    )
    kwargs.update(overrides)
    return db.create_obligation(**kwargs)


def test_create_returns_id_and_round_trips_every_field(db):
    oid = _create(db)
    assert isinstance(oid, str) and oid
    row = db.get_obligation(oid)
    assert row["kind"] == "reply"
    assert row["platform"] == "telegram"
    assert row["chat_id"] == "42"
    assert row["thread_id"] == "7"
    assert row["reply_to"] == "99"
    assert row["content"] == "hello"
    assert row["metadata"] == {"notify": True}
    assert row["session_id"] == "sess-1"
    assert row["status"] == "sending"          # the live send is in flight
    assert row["attempts"] == 0
    assert row["created_at"] == T0
    assert row["next_attempt_at"] == T0


def test_delivered_settles_terminally_with_message_id(db):
    oid = _create(db)
    db.settle_obligation_delivered(oid, message_id="m-1", now=T0 + 1)
    row = db.get_obligation(oid)
    assert row["status"] == "delivered"
    assert row["message_id"] == "m-1"
    assert row["delivered_at"] == T0 + 1
    # terminal rows are never due again
    assert db.claim_due_obligations(now=T0 + 10_000) == []


def test_failed_with_retry_requeues_and_counts_the_attempt(db):
    oid = _create(db)
    db.settle_obligation_failed(oid, "boom", retry_at=T0 + 30, now=T0 + 1)
    row = db.get_obligation(oid)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["last_error"] == "boom"
    assert row["next_attempt_at"] == T0 + 30


def test_failed_without_retry_abandons(db):
    oid = _create(db)
    db.settle_obligation_failed(oid, "gave up", retry_at=None, now=T0 + 1)
    row = db.get_obligation(oid)
    assert row["status"] == "abandoned"
    assert row["attempts"] == 1
    assert db.claim_due_obligations(now=T0 + 10_000) == []


def test_claim_returns_only_due_pending_rows_oldest_first_and_flips_them(db):
    late = _create(db, content="late")
    early = _create(db, content="early", now=T0 - 5)
    future = _create(db, content="future")
    for oid, due in ((late, T0 + 10), (early, T0 + 10), (future, T0 + 500)):
        db.settle_obligation_failed(oid, "net", retry_at=due, now=T0)

    claimed = db.claim_due_obligations(now=T0 + 20)
    assert [r["content"] for r in claimed] == ["early", "late"]
    assert all(r["status"] == "sending" for r in claimed)
    # a second claim at the same instant finds nothing: the flip is atomic
    assert db.claim_due_obligations(now=T0 + 20) == []
    assert db.get_obligation(future)["status"] == "pending"


def test_claim_reclaims_sending_rows_left_behind_by_a_dead_process(db):
    stuck = _create(db, now=T0)               # 'sending', never settled
    fresh = _create(db, now=T0 + 100)         # a live send still in flight
    claimed = db.claim_due_obligations(now=T0 + 130, stale_sending_after=120)
    assert [r["id"] for r in claimed] == [stuck]
    assert db.get_obligation(fresh)["status"] == "sending"


def test_release_requeues_without_counting_an_attempt(db):
    oid = _create(db)
    db.release_obligation(oid, retry_at=T0 + 15, now=T0 + 1)
    row = db.get_obligation(oid)
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == T0 + 15


def test_list_filters_by_status_and_purge_drops_only_old_terminal_rows(db):
    done = _create(db, content="done")
    gone = _create(db, content="gone")
    live = _create(db, content="live")
    db.settle_obligation_delivered(done, now=T0 + 1)
    db.settle_obligation_failed(gone, "x", retry_at=None, now=T0 + 1)
    db.settle_obligation_failed(live, "x", retry_at=T0 + 60, now=T0 + 1)

    assert {r["content"] for r in db.list_obligations()} == {"done", "gone", "live"}
    assert [r["content"] for r in db.list_obligations(status="pending")] == ["live"]

    removed = db.purge_obligations(older_than=T0 + 2)
    assert removed == 2
    assert [r["content"] for r in db.list_obligations()] == ["live"]


def test_list_filters_by_several_statuses_in_the_query_not_after_the_limit(db):
    for i in range(30):
        db.settle_obligation_delivered(_create(db, content=f"old{i}", now=T0 - 100 + i), now=T0)
    owed = _create(db, content="still owed", now=T0)
    db.settle_obligation_failed(owed, "net", retry_at=T0 + 60, now=T0 + 1)
    rows = db.list_obligations(status=("pending", "sending", "abandoned"), limit=10)
    assert [r["content"] for r in rows] == ["still owed"]


def test_expedite_moves_only_pending_rows_forward(db):
    waiting = _create(db, content="waiting")
    db.settle_obligation_failed(waiting, "net", retry_at=T0 + 600, now=T0)
    in_flight = _create(db, content="in flight", now=T0 + 5)        # live send, must stay claimed
    done = _create(db, content="done")
    db.settle_obligation_delivered(done, now=T0)

    changed = db.expedite_pending_obligations(now=T0 + 10)
    assert changed == 1
    assert db.get_obligation(waiting)["next_attempt_at"] == T0 + 10
    assert db.get_obligation(waiting)["status"] == "pending"
    assert db.get_obligation(in_flight)["status"] == "sending"
    assert db.get_obligation(in_flight)["updated_at"] == T0 + 5
    assert db.get_obligation(done)["status"] == "delivered"


def test_claim_is_safe_under_concurrent_callers(db):
    for i in range(20):
        oid = _create(db, content=f"c{i}")
        db.settle_obligation_failed(oid, "net", retry_at=T0, now=T0)
    seen, lock = [], threading.Lock()

    def worker():
        rows = db.claim_due_obligations(now=T0 + 1, limit=50)
        with lock:
            seen.extend(r["id"] for r in rows)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 20 and len(set(seen)) == 20
