"""Follow-ups queued during a restart drain survive into the next process.

With ``display.busy_input_mode`` set to ``queue`` or ``steer``, a message
that reaches a busy session while the gateway drains for a restart is
acknowledged as "queued for the next turn after it comes back".  The
in-memory pending slot does not survive the process exit, so the event is
also written under the profile's Janus home and replayed, once, by the
next gateway process through the normal inbound path.
"""

import asyncio
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.runner as gateway_runner
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, build_session_key
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)


def _draining_runner(mode: str = "queue"):
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = mode
    return runner, adapter


def _follow_up(text: str = "follow up", chat_id: str = "123456", chat_type: str = "dm", message_id: str = "m2"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id=chat_id, chat_type=chat_type),
        message_id=message_id,
    )


async def _queue_via_busy_guard(runner, adapter, event):
    """Arrive while the adapter's session guard is held (Level-1 busy path)."""
    adapter._active_sessions[build_session_key(event.source)] = asyncio.Event()
    await adapter.handle_message(event)
    return adapter.sent[-1] if adapter.sent else ""


async def _queue_via_running_agent(runner, adapter, event):
    """Arrive while the runner has an agent for the session (PRIORITY path)."""
    runner._running_agents[build_session_key(event.source)] = MagicMock()
    return await runner._handle_message(event)


async def _restart_into_fresh_runner():
    """Build the next gateway process's runner on the same JANUS_HOME."""
    runner, adapter = make_restart_runner()
    adapter.handle_message = AsyncMock()
    return runner, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("arrive", [_queue_via_busy_guard, _queue_via_running_agent])
async def test_follow_up_queued_during_restart_drain_is_replayed_once_after_restart(arrive):
    old_runner, old_adapter = _draining_runner()
    ack = await arrive(old_runner, old_adapter, _follow_up())
    assert "queued for the next turn" in ack

    # Drain timeout / process exit: everything in memory is gone.
    await old_adapter.cancel_background_tasks()
    old_runner._pending_messages.clear()

    runner, adapter = await _restart_into_fresh_runner()
    runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    adapter.handle_message.assert_awaited_once()
    replayed = adapter.handle_message.await_args.args[0]
    assert replayed.text == "follow up"
    assert replayed.message_id == "m2"
    assert replayed.source.chat_id == "123456"
    assert replayed.source.user_id == "u1"
    # A real inbound message, not a synthetic one that skips authorization.
    assert replayed.internal is False

    # A later restart (or a crash loop) must not deliver it a second time.
    runner_again, adapter_again = await _restart_into_fresh_runner()
    runner_again._schedule_resume_pending_sessions()
    await asyncio.sleep(0)
    adapter_again.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_up_queued_outside_a_drain_is_not_carried_to_the_next_process(monkeypatch):
    monkeypatch.setenv("JANUS_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner, adapter = make_restart_runner()
    runner._busy_input_mode = "queue"

    await _queue_via_busy_guard(runner, adapter, _follow_up(chat_id="chat-a", message_id="a"))
    await _queue_via_running_agent(runner, adapter, _follow_up(chat_id="chat-b", message_id="b"))
    # Both went into the normal in-memory queue of this process...
    assert {e.message_id for e in adapter._pending_messages.values()} == {"a", "b"}

    # ...and are not replayed by another process.
    next_runner, next_adapter = await _restart_into_fresh_runner()
    next_runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)
    next_adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_drain_queued_follow_up_is_dropped_not_replayed():
    old_runner, old_adapter = _draining_runner()
    await _queue_via_busy_guard(old_runner, old_adapter, _follow_up(text="old", chat_id="chat-old", message_id="old"))

    # Age that entry past the replay window, as if the gateway stayed down.
    path = GatewayRunner._drain_queue_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.values():
        entry["queued_at"] = time.time() - GatewayRunner._DRAIN_QUEUE_MAX_AGE_SECS - 60
    path.write_text(json.dumps(data), encoding="utf-8")

    await _queue_via_busy_guard(old_runner, old_adapter, _follow_up(text="new", chat_id="chat-new", message_id="new"))

    runner, adapter = await _restart_into_fresh_runner()
    runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert [call.args[0].text for call in adapter.handle_message.await_args_list] == ["new"]

    # The stale entry is gone too, not left behind for a later pass.
    runner_again, adapter_again = await _restart_into_fresh_runner()
    runner_again._schedule_resume_pending_sessions()
    await asyncio.sleep(0)
    adapter_again.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_replayed_follow_up_from_a_no_longer_authorized_sender_is_refused():
    old_runner, old_adapter = _draining_runner()
    # Group chat: an unauthorized sender is ignored silently (no pairing DM).
    await _queue_via_busy_guard(old_runner, old_adapter, _follow_up(chat_id="group-1", chat_type="group"))

    adapter = RestartTestAdapter()
    runner, _ = make_restart_runner(adapter)
    adapter.set_message_handler(runner._handle_message)
    runner._is_user_authorized = MagicMock(return_value=False)
    runner._handle_message_with_agent = AsyncMock(return_value="agent reply")

    runner._schedule_resume_pending_sessions()
    for _ in range(3):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            break
        await asyncio.wait(pending, timeout=5)

    runner._is_user_authorized.assert_called()
    checked = runner._is_user_authorized.call_args.args[0]
    assert (checked.chat_id, checked.user_id) == ("group-1", "u1")
    runner._handle_message_with_agent.assert_not_awaited()
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_replayed_follow_up_replaces_the_empty_auto_resume_turn():
    """A session interrupted by the drain timeout gets one turn, the real one."""
    old_runner, old_adapter = _draining_runner()
    event = _follow_up()
    await _queue_via_busy_guard(old_runner, old_adapter, event)

    runner, adapter = await _restart_into_fresh_runner()
    session_key = build_session_key(event.source)
    runner.session_store._entries = {
        session_key: SessionEntry(
            session_key=session_key,
            session_id="sid",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=event.source,
            platform=Platform.TELEGRAM,
            chat_type="dm",
            resume_pending=True,
            resume_reason="restart_timeout",
            last_resume_marked_at=datetime.now(),
        )
    }

    runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    adapter.handle_message.assert_awaited_once()
    replayed = adapter.handle_message.await_args.args[0]
    assert replayed.text == "follow up"
    assert replayed.internal is False


@pytest.mark.asyncio
async def test_follow_up_waits_for_its_platform_to_reconnect():
    old_runner, old_adapter = _draining_runner()
    await _queue_via_busy_guard(old_runner, old_adapter, _follow_up())

    runner, adapter = await _restart_into_fresh_runner()
    runner.adapters = {}  # Telegram failed to connect at startup
    runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)
    adapter.handle_message.assert_not_awaited()

    runner.adapters = {Platform.TELEGRAM: adapter}  # reconnect watcher brought it back
    runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM)
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "follow up"


@pytest.mark.asyncio
async def test_drain_ack_does_not_promise_a_turn_it_could_not_persist(monkeypatch):
    def _disk_full(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(gateway_runner, "atomic_json_write", _disk_full)
    runner, adapter = _draining_runner()

    ack = await _queue_via_busy_guard(runner, adapter, _follow_up())

    assert "queued" not in ack
    assert "not accepting another turn" in ack
