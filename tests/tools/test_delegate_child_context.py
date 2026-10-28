"""The child's conversation must see the caller's session context (PR #27 review).

``_run_single_child`` runs ``child.run_conversation`` on a one-shot executor
thread; without copying ``contextvars`` across that boundary the child's tools
lose ``JANUS_SESSION_*`` routing and the runtime-cwd override.
"""
import threading
from unittest.mock import MagicMock

import pytest

import tools.delegate_tool as delegate_tool
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars


@pytest.fixture(autouse=True)
def _no_env_fallback(monkeypatch):
    monkeypatch.delenv("JANUS_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("JANUS_SESSION_PLATFORM", raising=False)


def _child(seen):
    child = MagicMock()
    child.session_id = "child-ctx"
    child._subagent_id = "sa-ctx"
    child._delegate_saved_tool_names = None
    child._credential_pool = None
    child.tool_progress_callback = None
    child.model = "test/model"
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.get_activity_summary.return_value = {"api_call_count": 1}

    def _run_conversation(**_kw):
        seen["chat_id"] = get_session_env("JANUS_SESSION_CHAT_ID", "")
        seen["thread"] = threading.current_thread().name
        return {"final_response": "ok", "completed": True, "api_calls": 1, "messages": []}

    child.run_conversation.side_effect = _run_conversation
    return child


def _parent():
    parent = MagicMock()
    parent.session_id = "parent-ctx"
    parent._current_task_id = None
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


def test_run_single_child_carries_the_callers_session_context_into_the_child():
    seen = {}
    tokens = set_session_vars(platform="telegram", chat_id="42", session_key="agent:main:telegram:dm:42")
    try:
        result = delegate_tool._run_single_child(0, "do it", child=_child(seen), parent_agent=_parent())
    finally:
        clear_session_vars(tokens)
    assert result["status"] == "completed"
    assert seen["thread"] != threading.current_thread().name   # it really ran on the executor
    assert seen["chat_id"] == "42"
