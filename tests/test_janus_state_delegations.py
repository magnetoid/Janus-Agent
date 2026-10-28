"""Storage primitives for durable background delegations (plan item 2.3).

A delegation row is the handle ``delegate_task(background=true)`` returns.
It outlives the parent turn: the child's transcript streams to its own
session (``sessions.parent_session_id``) and this row tracks lifecycle,
routing for result delivery, and the worker process that owns it. Policy
(threads, notifications, orphan detection) lives in
``agent.background_delegation`` and is tested there.
"""
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
        parent_session_id="parent-1", child_session_id="child-1",
        goal="summarise the repo", role="leaf", model="gpt-x",
        subagent_id="sa-0-abc", pid=4242,
        route={"platform": "telegram", "chat_id": "42", "session_key": "telegram:dm:42"},
        now=T0,
    )
    kwargs.update(overrides)
    return db.create_delegation(**kwargs)


def test_create_returns_id_and_round_trips_every_field(db):
    did = _create(db)
    assert isinstance(did, str) and did
    row = db.get_delegation(did)
    assert row["parent_session_id"] == "parent-1"
    assert row["child_session_id"] == "child-1"
    assert row["goal"] == "summarise the repo"
    assert row["role"] == "leaf"
    assert row["model"] == "gpt-x"
    assert row["subagent_id"] == "sa-0-abc"
    assert row["pid"] == 4242
    assert row["route"] == {"platform": "telegram", "chat_id": "42", "session_key": "telegram:dm:42"}
    assert row["status"] == "running"
    assert row["stop_requested"] is False
    assert row["created_at"] == T0
    assert row["finished_at"] is None
    assert row["delivered_at"] is None


def test_finish_records_outcome_and_is_terminal(db):
    did = _create(db)
    db.finish_delegation(
        did, status="completed", summary="all good", api_calls=3,
        cost_usd=0.012, now=T0 + 30,
    )
    row = db.get_delegation(did)
    assert row["status"] == "completed"
    assert row["summary"] == "all good"
    assert row["api_calls"] == 3
    assert row["cost_usd"] == pytest.approx(0.012)
    assert row["finished_at"] == T0 + 30
    # a second finish must not overwrite the first outcome
    db.finish_delegation(did, status="failed", error="late", now=T0 + 60)
    row = db.get_delegation(did)
    assert row["status"] == "completed" and row["finished_at"] == T0 + 30


def test_finish_with_error_keeps_the_error_text(db):
    did = _create(db)
    db.finish_delegation(did, status="failed", error="boom", now=T0 + 1)
    row = db.get_delegation(did)
    assert row["status"] == "failed"
    assert row["error"] == "boom"


def test_delivered_is_recorded_once(db):
    did = _create(db)
    db.finish_delegation(did, status="completed", summary="x", now=T0 + 1)
    assert db.mark_delegation_delivered(did, now=T0 + 2) is True
    assert db.mark_delegation_delivered(did, now=T0 + 3) is False
    assert db.get_delegation(did)["delivered_at"] == T0 + 2


def test_stop_request_flag_is_visible_to_the_owning_process(db):
    did = _create(db)
    assert db.request_delegation_stop(did, now=T0 + 1) is True
    assert db.get_delegation(did)["stop_requested"] is True
    # stopping something already finished is a no-op
    done = _create(db, child_session_id="child-2")
    db.finish_delegation(done, status="completed", now=T0 + 1)
    assert db.request_delegation_stop(done, now=T0 + 2) is False
    assert db.get_delegation(done)["stop_requested"] is False


def test_list_filters_by_status_and_parent_newest_first(db):
    a = _create(db, child_session_id="c-a", now=T0)
    b = _create(db, child_session_id="c-b", now=T0 + 1, parent_session_id="parent-2")
    c = _create(db, child_session_id="c-c", now=T0 + 2)
    db.finish_delegation(b, status="completed", now=T0 + 5)

    assert [r["id"] for r in db.list_delegations()] == [c, b, a]
    assert [r["id"] for r in db.list_delegations(status="running")] == [c, a]
    assert [r["id"] for r in db.list_delegations(status=("completed", "failed"))] == [b]
    assert [r["id"] for r in db.list_delegations(parent_session_id="parent-2")] == [b]
    assert [r["id"] for r in db.list_delegations(limit=1)] == [c]


def test_running_delegations_owned_by_a_pid_can_be_marked_orphaned(db):
    mine = _create(db, child_session_id="c-mine", pid=111)
    gone = _create(db, child_session_id="c-gone", pid=222)
    done = _create(db, child_session_id="c-done", pid=222)
    db.finish_delegation(done, status="completed", now=T0 + 1)

    orphaned = db.orphan_delegations(pids=[222], now=T0 + 10, error="worker process exited")
    assert [r["id"] for r in orphaned] == [gone]
    assert db.get_delegation(gone)["status"] == "orphaned"
    assert db.get_delegation(gone)["error"] == "worker process exited"
    assert db.get_delegation(gone)["finished_at"] == T0 + 10
    assert db.get_delegation(mine)["status"] == "running"
    assert db.get_delegation(done)["status"] == "completed"


def test_running_pids_lists_distinct_owners_of_live_rows(db):
    _create(db, child_session_id="c1", pid=111)
    _create(db, child_session_id="c2", pid=111)
    _create(db, child_session_id="c3", pid=333)
    done = _create(db, child_session_id="c4", pid=444)
    db.finish_delegation(done, status="completed", now=T0 + 1)
    assert sorted(db.running_delegation_pids()) == [111, 333]
