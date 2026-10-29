"""Tool progress reporting and MCP notifications/progress (plan item 3.6)."""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import tool_progress


def _agent():
    calls = []
    agent = SimpleNamespace(
        tool_progress_callback=lambda *a, **k: calls.append((a, k)),
        _touch_activity=MagicMock(),
    )
    return agent, calls


def test_report_without_a_bound_agent_is_a_noop():
    tool_progress.unbind()
    tool_progress.report_progress("mcp_srv_tool", "working", 1, 2)   # must not raise


def test_bound_agent_receives_a_tool_progress_event_and_activity():
    agent, calls = _agent()
    tool_progress.bind_agent(agent)
    try:
        tool_progress.report_progress("mcp_srv_tool", "half way", 5, 10)
    finally:
        tool_progress.unbind()
    assert calls == [(("tool.progress", "mcp_srv_tool", "half way", None),
                      {"progress": 5, "total": 10})]
    agent._touch_activity.assert_called()


def test_binding_follows_the_turn_into_worker_threads():
    from tools.thread_context import propagate_context_to_thread
    agent, calls = _agent()
    tool_progress.bind_agent(agent)
    try:
        t = threading.Thread(target=propagate_context_to_thread(
            lambda: tool_progress.report_progress("t", "x", 1, None)))
        t.start()
        t.join(5)
    finally:
        tool_progress.unbind()
    assert calls and calls[0][0][0] == "tool.progress"


def test_mcp_progress_notifications_reach_the_agent(monkeypatch, tmp_path):
    pytest.importorskip("mcp.client.auth.oauth2")
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    from tools import mcp_tool
    from tests.tools.test_mcp_circuit_breaker import _cleanup, _install_stub_server

    async def _call_tool(name, arguments=None, progress_callback=None, **kw):
        assert progress_callback is not None
        await progress_callback(3, 10, "indexing")
        result = MagicMock()
        result.isError = False
        block = MagicMock()
        block.text = "done"
        result.content = [block]
        result.structuredContent = None
        return result

    _install_stub_server(mcp_tool, "srv", _call_tool)
    mcp_tool._ensure_mcp_loop()
    agent, calls = _agent()
    tool_progress.bind_agent(agent)
    try:
        out = mcp_tool._make_tool_handler("srv", "index", 10)({})
    finally:
        tool_progress.unbind()
        _cleanup(mcp_tool, "srv")
    assert "done" in out
    events = [c for c in calls if c[0][0] == "tool.progress"]
    assert events and events[0][0][2] == "indexing"
    assert events[0][1] == {"progress": 3, "total": 10}


def test_cli_spinner_shows_progress_for_the_running_tool():
    import cli
    obj = SimpleNamespace(_spinner_text="🔌 index", _invalidate=MagicMock(),
                          _tool_start_time=1.0, tool_progress_mode="all",
                          _pending_tool_info={}, _last_scrollback_tool=None)
    cli.JanusCLI._on_tool_progress(obj, "tool.progress", "mcp_srv_index", "indexing",
                                   None, progress=3, total=10)
    assert "indexing" in obj._spinner_text and "3/10" in obj._spinner_text
