"""`delegate_task(background=true)` — handle now, result later (plan item 2.3)."""
import json
import threading
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as delegate_tool
from agent import background_delegation as bg
from agent.background_delegation import BackgroundDelegationManager
from janus_state import SessionDB

T0 = 1_800_000_000.0


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def sink():
    return []


@pytest.fixture()
def manager(db, sink, monkeypatch):
    mgr = BackgroundDelegationManager(db=db, sink=sink.append, clock=lambda: T0,
                                      poll_interval=0.02, max_running=2)
    monkeypatch.setattr(bg, "get_manager", lambda *a, **k: mgr)
    return mgr


@pytest.fixture()
def release():
    return threading.Event()


@pytest.fixture(autouse=True)
def _stubs(monkeypatch, release):
    """Fake child construction and child run; neither imports run_agent."""
    built = []

    def _fake_build_child(task_index, **kwargs):
        child = MagicMock()
        child.session_id = f"child-{task_index}"
        child._subagent_id = f"sa-{task_index}-x"
        child._delegate_saved_tool_names = []
        child._credential_pool = None
        parent = kwargs["parent_agent"]
        with parent._active_children_lock:
            parent._active_children.append(child)      # what the real builder does
        built.append(child)
        return child

    def _fake_run_single_child(task_index, goal, child=None, parent_agent=None, **_):
        release.wait(5)
        return {"task_index": task_index, "status": "completed", "summary": f"did {goal}",
                "api_calls": 1, "_child_cost_usd": 0.01, "model": "m"}

    monkeypatch.setattr(delegate_tool, "_build_child_agent", _fake_build_child)
    monkeypatch.setattr(delegate_tool, "_run_single_child", _fake_run_single_child)
    monkeypatch.setattr(delegate_tool, "_load_config",
                        lambda: {"background": {"enabled": True, "max_running": 2}})
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda *a, **k: None)
    return built


def _parent():
    parent = MagicMock()
    parent.session_id = "parent-1"
    parent.platform = "cli"
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent.session_estimated_cost_usd = 0.0
    return parent


def _wait(pred, timeout=5.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_background_returns_a_handle_while_the_child_is_still_running(manager, db, release):
    parent = _parent()
    out = json.loads(delegate_tool.delegate_task(goal="scan the repo", parent_agent=parent,
                                                 background=True))
    assert out["background"] is True
    handle = out["delegations"][0]
    assert handle["status"] == "running"
    assert handle["child_session_id"] == "child-0"
    assert db.get_delegation(handle["id"])["status"] == "running"
    assert manager.running_count() == 1
    # the parent's turn can end, be interrupted or reset without killing the child
    assert parent._active_children == []
    release.set()
    assert _wait(lambda: manager.running_count() == 0)


def test_result_reaches_the_parent_after_the_child_finishes(manager, db, sink, release):
    parent = _parent()
    out = json.loads(delegate_tool.delegate_task(goal="scan the repo", parent_agent=parent,
                                                 background=True))
    assert sink == []
    release.set()
    assert _wait(lambda: len(sink) == 1)
    evt = sink[0]
    assert evt["type"] == "delegation_complete"
    assert evt["delegation_id"] == out["delegations"][0]["id"]
    assert evt["summary"] == "did scan the repo"
    assert evt["parent_session_id"] == "parent-1"
    assert db.get_delegation(evt["delegation_id"])["status"] == "completed"
    assert parent.session_estimated_cost_usd == pytest.approx(0.01)


def test_batch_background_returns_one_handle_per_task(manager, release):
    out = json.loads(delegate_tool.delegate_task(
        tasks=[{"goal": "a"}, {"goal": "b"}], parent_agent=_parent(), background=True,
    ))
    assert [h["goal"] for h in out["delegations"]] == ["a", "b"]
    assert {h["child_session_id"] for h in out["delegations"]} == {"child-0", "child-1"}
    release.set()
    assert _wait(lambda: manager.running_count() == 0)


def test_background_is_refused_when_disabled_in_config(manager, monkeypatch, db):
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"background": {"enabled": False}})
    out = json.loads(delegate_tool.delegate_task(goal="x", parent_agent=_parent(), background=True))
    assert "error" in out and "delegation.background.enabled" in out["error"]
    assert db.list_delegations() == []


def test_background_still_honours_the_spend_cap(manager, monkeypatch, db):
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason",
                        lambda *a, **k: "daily spend cap exceeded")
    out = json.loads(delegate_tool.delegate_task(goal="x", parent_agent=_parent(), background=True))
    assert "error" in out and "spend cap" in out["error"]
    assert db.list_delegations() == []


def test_running_cap_is_reported_as_a_tool_error(manager, release, db):
    delegate_tool.delegate_task(tasks=[{"goal": "a"}, {"goal": "b"}],
                                parent_agent=_parent(), background=True)
    out = json.loads(delegate_tool.delegate_task(goal="c", parent_agent=_parent(), background=True))
    assert "error" in out and "max_running" in out["error"]
    assert len(db.list_delegations()) == 2
    release.set()


def test_gateway_routing_is_captured_from_the_parent_thread(manager, db, release):
    from gateway.session_context import clear_session_vars, set_session_vars
    tokens = set_session_vars(platform="telegram", chat_id="42", thread_id="7",
                              session_key="telegram:dm:42", user_id="u1", user_name="ann")
    try:
        out = json.loads(delegate_tool.delegate_task(goal="x", parent_agent=_parent(),
                                                     background=True))
    finally:
        clear_session_vars(tokens)
    route = db.get_delegation(out["delegations"][0]["id"])["route"]
    assert route == {"platform": "telegram", "chat_id": "42", "thread_id": "7",
                     "session_key": "telegram:dm:42", "user_id": "u1", "user_name": "ann"}
    release.set()


def test_foreground_delegation_is_unchanged_by_the_new_parameter(manager, db, release):
    release.set()
    out = json.loads(delegate_tool.delegate_task(goal="x", parent_agent=_parent()))
    assert "results" in out and "background" not in out
    assert db.list_delegations() == []


def test_schema_and_both_dispatch_sites_carry_background():
    params = cast(dict, delegate_tool.DELEGATE_TASK_SCHEMA["parameters"])
    assert params["properties"]["background"]["type"] == "boolean"

    seen = {}
    with patch.object(delegate_tool, "delegate_task",
                      side_effect=lambda **kw: seen.update(kw) or "{}"):
        entry = delegate_tool.registry.get_entry("delegate_task")
        assert entry is not None
        entry.handler({"goal": "g", "background": True}, parent_agent="P")
    assert seen["background"] is True and seen["parent_agent"] == "P"

    seen.clear()
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    with patch("tools.delegate_tool.delegate_task", side_effect=lambda **kw: seen.update(kw) or "{}"):
        agent._dispatch_delegate_task({"goal": "g", "background": True})
    assert seen["background"] is True and seen["parent_agent"] is agent


def test_foreground_batch_children_see_the_callers_session_context(manager, monkeypatch, release):
    """Batch children run on pool threads; the caller's contextvars must travel with them."""
    import os
    from gateway.session_context import clear_session_vars, get_session_env, set_session_vars
    monkeypatch.delenv("JANUS_SESSION_CHAT_ID", raising=False)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_concurrent_children": 2})
    seen = {}

    def _record(task_index, goal, child=None, parent_agent=None, **_):
        seen[task_index] = get_session_env("JANUS_SESSION_CHAT_ID", "")
        return {"task_index": task_index, "status": "completed", "summary": "ok",
                "api_calls": 1, "_child_cost_usd": 0.0, "model": "m"}

    monkeypatch.setattr(delegate_tool, "_run_single_child", _record)
    release.set()
    tokens = set_session_vars(platform="telegram", chat_id="42", session_key="agent:main:telegram:dm:42")
    try:
        out = json.loads(delegate_tool.delegate_task(tasks=[{"goal": "a"}, {"goal": "b"}],
                                                     parent_agent=_parent()))
    finally:
        clear_session_vars(tokens)
    assert "results" in out
    assert seen == {0: "42", 1: "42"}
    assert "JANUS_SESSION_CHAT_ID" not in os.environ


def test_background_children_flush_their_transcript_every_iteration(manager, release, _stubs):
    delegate_tool.delegate_task(goal="scan", parent_agent=_parent(), background=True)
    assert _stubs[0]._flush_db_each_iteration is True
    release.set()
    assert _wait(lambda: manager.running_count() == 0)
