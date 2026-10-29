"""Background delegation results reach the parent on every surface.

The CLI and TUI drain ``process_registry.completion_queue`` and format each
event with ``format_process_notification``; the gateway registers a sink
and injects an internal message routed by the delegation's stored route.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import background_delegation as bg
from gateway.core import _format_gateway_process_notification
from gateway.config import Platform
from gateway.runner import GatewayRunner
from tools.process_registry import format_process_notification


def _evt(kind="delegation_complete", **over):
    evt = {
        "type": kind, "session_id": "d1", "delegation_id": "d1", "goal": "scan the repo",
        "status": "completed", "summary": "3 files changed", "child_session_id": "child-9",
        "session_key": "agent:main:telegram:dm:42", "platform": "telegram", "chat_id": "42",
    }
    evt.update(over)
    return evt


def test_cli_and_tui_formatter_renders_delegation_events():
    text = format_process_notification(_evt()) or ""
    assert text.startswith("[IMPORTANT: Background delegation d1 completed")
    assert "scan the repo" in text and "3 files changed" in text and "child-9" in text
    lost = format_process_notification(_evt("delegation_orphaned", status="orphaned")) or ""
    assert "lost" in lost and "child-9" in lost


def test_gateway_formatter_renders_delegation_events_too():
    text = _format_gateway_process_notification(_evt())
    assert text and "3 files changed" in text
    # unrelated completion events are still left to the process watcher
    assert _format_gateway_process_notification({"type": "completion", "session_id": "p"}) is None


def _runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = MagicMock()
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = MagicMock(_entries={})
    runner._get_cached_session_source = lambda key: None
    runner._gateway_loop = None
    return runner, adapter


def test_gateway_injects_an_internal_message_routed_by_the_event():
    runner, adapter = _runner()
    asyncio.run(runner._inject_delegation_notification(_evt(thread_id="7")))
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.internal is True
    assert "3 files changed" in event.text
    assert event.source.platform == Platform.TELEGRAM
    assert event.source.chat_id == "42"
    assert event.source.thread_id == "7"


def test_gateway_sink_schedules_injection_on_its_loop(monkeypatch):
    runner, adapter = _runner()
    bg.reset_for_tests()
    manager = bg.BackgroundDelegationManager(db=MagicMock())
    monkeypatch.setattr(bg, "get_manager", lambda *a, **k: manager)

    async def go():
        runner._gateway_loop = asyncio.get_running_loop()
        runner._register_background_delegation_sink()
        # a worker thread would call the sink; call it directly off-loop
        await asyncio.get_running_loop().run_in_executor(None, manager._emit, _evt())
        for _ in range(50):
            if adapter.handle_message.await_count:
                break
            await asyncio.sleep(0.02)

    asyncio.run(go())
    adapter.handle_message.assert_awaited_once()
    bg.reset_for_tests()


def test_gateway_start_announces_delegations_lost_in_a_previous_run(monkeypatch, tmp_path):
    from janus_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_delegation(goal="lost work", child_session_id="c-lost", pid=999_999_999,
                         route={"session_key": "agent:main:telegram:dm:42", "platform": "telegram",
                                "chat_id": "42"})
    bg.reset_for_tests()
    manager = bg.BackgroundDelegationManager(db=db, pid=1)
    monkeypatch.setattr(bg, "get_manager", lambda *a, **k: manager)
    runner, adapter = _runner()

    async def go():
        runner._gateway_loop = asyncio.get_running_loop()
        runner._register_background_delegation_sink()
        runner._announce_orphaned_delegations(pid_alive=lambda pid: False)
        for _ in range(50):
            if adapter.handle_message.await_count:
                break
            await asyncio.sleep(0.02)

    asyncio.run(go())
    event = adapter.handle_message.await_args.args[0]
    assert "lost work" in event.text and "c-lost" in event.text
    assert db.list_delegations()[0]["status"] == "orphaned"
    db.close()
    bg.reset_for_tests()
