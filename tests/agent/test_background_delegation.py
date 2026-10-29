"""Runtime policy for durable background delegations (plan item 2.3).

``BackgroundDelegationManager`` owns the worker thread for a
``delegate_task(background=true)`` child: it records the handle before the
child starts, folds the outcome into the ``delegations`` table when the
child finishes, and hands a completion event to whichever surface is
listening (the process-wide ``completion_queue`` that the CLI and TUI
already drain, or a sink the gateway registers). Stop requests reach the
child from this process or from another one through the database row.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from agent import background_delegation as bg
from agent.background_delegation import (
    BackgroundDelegationManager,
    BackgroundLimitError,
    format_delegation_notification,
)
from janus_state import SessionDB

T0 = 1_800_000_000.0


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


class FakeChild:
    def __init__(self, session_id="child-1"):
        self.session_id = session_id
        self.interrupted = []

    def interrupt(self, message=None):
        self.interrupted.append(message)


def _parent():
    return SimpleNamespace(session_id="parent-1", session_estimated_cost_usd=0.0)


def _entry(**over):
    e = {"status": "completed", "summary": "done: 3 files", "api_calls": 2,
         "_child_cost_usd": 0.05, "model": "m"}
    e.update(over)
    return e


def _manager(db, **kw):
    kw.setdefault("clock", lambda: T0)
    kw.setdefault("poll_interval", 0.02)
    return BackgroundDelegationManager(db=db, **kw)


def _wait(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_start_returns_a_handle_before_the_child_finishes(db):
    release = threading.Event()
    mgr = _manager(db)
    child = FakeChild()

    def run_child():
        release.wait(5)
        return _entry()

    handle = mgr.start(child=child, goal="summarise", parent_agent=_parent(),
                       run_child=run_child, role="leaf", model="m")
    assert handle["status"] == "running"
    assert handle["child_session_id"] == "child-1"
    assert db.get_delegation(handle["id"])["status"] == "running"
    assert mgr.running_count() == 1
    release.set()
    assert _wait(lambda: mgr.running_count() == 0)


def test_completion_is_recorded_and_announced_on_the_completion_queue(db):
    events = []
    mgr = _manager(db, sink=None)
    bg_queue = bg._default_queue()
    while not bg_queue.empty():
        bg_queue.get_nowait()
    child = FakeChild()
    parent = _parent()
    mgr.start(child=child, goal="summarise", parent_agent=parent,
              run_child=lambda: _entry(), role="leaf", model="m",
              route={"session_key": "cli:local"})
    assert _wait(lambda: mgr.running_count() == 0)
    row = db.list_delegations()[0]
    assert row["status"] == "completed"
    assert row["summary"] == "done: 3 files"
    assert row["api_calls"] == 2
    assert row["cost_usd"] == pytest.approx(0.05)
    assert row["delivered_at"] is not None
    assert parent.session_estimated_cost_usd == pytest.approx(0.05)

    assert _wait(lambda: not bg_queue.empty())
    while not bg_queue.empty():
        events.append(bg_queue.get_nowait())
    evt = events[-1]
    assert evt["type"] == "delegation_complete"
    assert evt["session_id"] == row["id"]          # dedup identity for the TUI poller
    assert evt["session_key"] == "cli:local"
    assert evt["child_session_id"] == "child-1"
    text = format_delegation_notification(evt)
    assert "[IMPORTANT:" in text and "summarise" in text and "done: 3 files" in text
    assert row["id"] in text and "child-1" in text


def test_sink_receives_events_instead_of_the_queue_with_routing_from_the_route(db):
    got = []
    bg_queue = bg._default_queue()
    while not bg_queue.empty():
        bg_queue.get_nowait()
    mgr = _manager(db, sink=got.append)
    route = {"platform": "telegram", "chat_id": "42", "thread_id": "7",
             "session_key": "telegram:dm:42", "user_id": "u1", "user_name": "ann"}
    mgr.start(child=FakeChild(), goal="g", parent_agent=_parent(),
              run_child=lambda: _entry(), role="leaf", model="m", route=route)
    assert _wait(lambda: len(got) == 1)
    evt = got[0]
    for k, v in route.items():
        assert evt[k] == v
    assert bg_queue.empty()


def test_child_exception_is_recorded_as_failed_and_still_announced(db):
    got = []
    mgr = _manager(db, sink=got.append)

    def boom():
        raise RuntimeError("provider exploded")

    mgr.start(child=FakeChild(), goal="g", parent_agent=_parent(),
              run_child=boom, role="leaf", model="m")
    assert _wait(lambda: len(got) == 1)
    row = db.list_delegations()[0]
    assert row["status"] == "failed"
    assert "provider exploded" in row["error"]
    assert "failed" in format_delegation_notification(got[0])


@pytest.mark.parametrize("child_status,expected", [
    ("completed", "completed"), ("failed", "failed"), ("timeout", "timeout"),
    ("interrupted", "interrupted"), ("error", "failed"),
])
def test_child_statuses_map_onto_the_delegation_row(db, child_status, expected):
    got = []
    mgr = _manager(db, sink=got.append)
    mgr.start(child=FakeChild(), goal="g", parent_agent=_parent(),
              run_child=lambda: _entry(status=child_status, error="e"),
              role="leaf", model="m")
    assert _wait(lambda: len(got) == 1)
    assert db.list_delegations()[0]["status"] == expected


def test_request_stop_interrupts_the_child_in_this_process(db):
    release = threading.Event()
    child = FakeChild()
    mgr = _manager(db, sink=lambda e: None)

    def run_child():
        release.wait(5)
        return _entry(status="interrupted")

    handle = mgr.start(child=child, goal="g", parent_agent=_parent(),
                       run_child=run_child, role="leaf", model="m")
    assert mgr.request_stop(handle["id"]) is True
    assert child.interrupted, "child.interrupt() was not called"
    assert db.get_delegation(handle["id"])["stop_requested"] is True
    release.set()
    assert _wait(lambda: db.get_delegation(handle["id"])["status"] == "interrupted")
    assert mgr.request_stop(handle["id"]) is False


def test_stop_requested_from_another_process_is_picked_up_by_polling(db):
    release = threading.Event()
    child = FakeChild()
    mgr = _manager(db, sink=lambda e: None)

    def run_child():
        release.wait(5)
        return _entry(status="interrupted")

    handle = mgr.start(child=child, goal="g", parent_agent=_parent(),
                       run_child=run_child, role="leaf", model="m")
    # another process (``janus delegations stop``) only has the database
    db.request_delegation_stop(handle["id"])
    assert _wait(lambda: bool(child.interrupted)), "poller never saw stop_requested"
    release.set()
    assert _wait(lambda: mgr.running_count() == 0)


def test_running_cap_is_enforced(db):
    release = threading.Event()
    mgr = _manager(db, sink=lambda e: None, max_running=1)

    def run_child():
        release.wait(5)
        return _entry()

    mgr.start(child=FakeChild("c1"), goal="g", parent_agent=_parent(),
              run_child=run_child, role="leaf", model="m")
    with pytest.raises(BackgroundLimitError):
        mgr.start(child=FakeChild("c2"), goal="g", parent_agent=_parent(),
                  run_child=run_child, role="leaf", model="m")
    assert len(db.list_delegations()) == 1       # the refused child left no row
    release.set()
    assert _wait(lambda: mgr.running_count() == 0)


def test_reconcile_marks_rows_of_dead_workers_orphaned_and_announces_them(db):
    got = []
    mgr = _manager(db, sink=got.append, pid=1000)
    db.create_delegation(goal="lost", child_session_id="c-lost", pid=999_999_999,
                         route={"session_key": "telegram:dm:1"}, now=T0 - 100)
    mine = db.create_delegation(goal="mine", child_session_id="c-mine", pid=1000, now=T0 - 50)

    orphaned = mgr.reconcile_orphans(pid_alive=lambda pid: pid == 1000)
    assert [r["goal"] for r in orphaned] == ["lost"]
    assert db.get_delegation(mine)["status"] == "running"
    assert len(got) == 1
    evt = got[0]
    assert evt["type"] == "delegation_orphaned"
    assert evt["session_key"] == "telegram:dm:1"
    text = format_delegation_notification(evt)
    assert "lost" in text and "c-lost" in text
    # a second pass has nothing left to announce
    assert mgr.reconcile_orphans(pid_alive=lambda pid: pid == 1000) == []
    assert len(got) == 1


def test_reconcile_without_announce_leaves_the_orphan_for_a_later_announcer(db):
    """`janus delegations list` must not consume the one announcement a lost
    delegation gets: it orphans the row quietly and the next announcing
    reconcile (gateway start-up) still delivers it, exactly once."""
    quiet = _manager(db, sink=lambda evt: None, pid=0)
    lost = db.create_delegation(goal="lost", child_session_id="c-lost", pid=999_999_999,
                                route={"session_key": "telegram:dm:1"}, now=T0 - 100)

    rows = quiet.reconcile_orphans(pid_alive=lambda pid: False, announce=False)
    assert [r["id"] for r in rows] == [lost]
    row = db.get_delegation(lost)
    assert row["status"] == "orphaned" and row["delivered_at"] is None

    got = []
    announcer = _manager(db, sink=got.append, pid=1000)
    assert announcer.reconcile_orphans(pid_alive=lambda pid: pid == 1000) == []
    assert [e["delegation_id"] for e in got] == [lost]
    assert got[0]["type"] == "delegation_orphaned"
    assert db.get_delegation(lost)["delivered_at"] is not None
    announcer.reconcile_orphans(pid_alive=lambda pid: pid == 1000)
    assert len(got) == 1
