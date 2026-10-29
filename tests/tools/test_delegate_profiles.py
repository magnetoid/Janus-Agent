"""`delegate_task(profile=...)` — named subagent profiles (plan item 3.2)."""
import json
import threading
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as delegate_tool

PROFILES = {
    "cheap": {
        "model": "cheap/model",
        "base_url": "http://localhost:1234/v1",
        "toolsets": ["web"],
        "max_iterations": 7,
        "role": "orchestrator",
        "budget_usd": 0.25,
    },
    "coder": {"model": "smart/model", "base_url": "http://localhost:4321/v1",
              "toolsets": ["terminal", "file"]},
    "caps-only": {"max_iterations": 3},
}


@pytest.fixture()
def built(monkeypatch):
    calls = []

    def _fake_build(task_index, **kwargs):
        child = MagicMock()
        child.session_id = f"child-{task_index}"
        child._session_cost_limit_usd_override = None
        calls.append({"index": task_index, "child": child, **kwargs})
        return child

    def _fake_run(task_index, goal, child=None, parent_agent=None, **_):
        return {"task_index": task_index, "status": "completed", "summary": "ok",
                "api_calls": 1, "model": "m"}

    monkeypatch.setattr(delegate_tool, "_build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_tool, "_run_single_child", _fake_run)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda *a, **k: None)
    monkeypatch.setattr(delegate_tool, "_get_max_concurrent_children", lambda: 3)
    _config({"model": "global/model", "base_url": "http://global:1/v1", "profiles": PROFILES},
            monkeypatch)
    return calls


def _config(cfg, monkeypatch):
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)


def _parent():
    parent = MagicMock()
    parent.session_id = "parent-1"
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


def _delegate(**kw):
    return json.loads(delegate_tool.delegate_task(parent_agent=_parent(), **kw))


def test_profile_supplies_model_toolsets_iterations_and_role(built):
    out = _delegate(goal="find docs", profile="cheap")
    assert "results" in out, out
    call = built[0]
    assert call["model"] == "cheap/model"
    assert call["override_base_url"] == "http://localhost:1234/v1"
    assert call["toolsets"] == ["web"]
    assert call["max_iterations"] == 7
    assert call["role"] == "orchestrator"


def test_explicit_arguments_beat_the_profile(built):
    _delegate(goal="find docs", profile="cheap", toolsets=["file"], role="leaf")
    call = built[0]
    assert call["toolsets"] == ["file"]
    assert call["role"] == "leaf"
    assert call["model"] == "cheap/model"     # not overridden by the call, so the profile's


def test_profile_budget_becomes_the_childs_session_cost_cap(built):
    _delegate(goal="find docs", profile="cheap")
    assert built[0]["child"]._session_cost_limit_usd_override == pytest.approx(0.25)


def test_profile_without_credentials_keeps_the_global_delegation_model(built):
    _delegate(goal="x", profile="caps-only")
    call = built[0]
    assert call["model"] == "global/model"
    assert call["override_base_url"] == "http://global:1/v1"
    assert call["max_iterations"] == 3


def test_no_profile_leaves_the_existing_behaviour_alone(built):
    _delegate(goal="x")
    call = built[0]
    assert call["model"] == "global/model"
    assert call["child"]._session_cost_limit_usd_override is None


def test_unknown_profile_is_rejected_with_the_available_names(built):
    out = _delegate(goal="x", profile="nope")
    assert "error" in out
    for name in PROFILES:
        assert name in out["error"]
    assert built == []


def test_batch_tasks_can_name_different_profiles(built):
    out = _delegate(tasks=[{"goal": "a", "profile": "cheap"}, {"goal": "b", "profile": "coder"},
                           {"goal": "c"}], profile="caps-only")
    assert len(out["results"]) == 3
    by_goal = {c["goal"]: c for c in built}
    assert by_goal["a"]["model"] == "cheap/model"
    assert by_goal["b"]["model"] == "smart/model"
    assert by_goal["b"]["toolsets"] == ["terminal", "file"]
    # the top-level profile is the default for tasks that do not name one
    assert by_goal["c"]["max_iterations"] == 3


def test_unknown_profile_inside_a_batch_builds_nothing(built):
    out = _delegate(tasks=[{"goal": "a", "profile": "cheap"}, {"goal": "b", "profile": "ghost"}])
    assert "error" in out and "ghost" in out["error"]
    assert built == []


def test_schema_and_both_dispatch_sites_carry_profile():
    params = cast(dict, delegate_tool.DELEGATE_TASK_SCHEMA["parameters"])
    assert params["properties"]["profile"]["type"] == "string"
    assert params["properties"]["tasks"]["items"]["properties"]["profile"]["type"] == "string"

    seen = {}
    with patch.object(delegate_tool, "delegate_task",
                      side_effect=lambda **kw: seen.update(kw) or "{}"):
        entry = delegate_tool.registry.get_entry("delegate_task")
        assert entry is not None
        entry.handler({"goal": "g", "profile": "cheap"}, parent_agent="P")
    assert seen["profile"] == "cheap"

    seen.clear()
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    with patch("tools.delegate_tool.delegate_task",
               side_effect=lambda **kw: seen.update(kw) or "{}"):
        agent._dispatch_delegate_task({"goal": "g", "profile": "cheap"})
    assert seen["profile"] == "cheap"
