"""A turn marks its session as in flight, so a crash mid-turn is recovered.

``suspend_recently_active()`` only catches sessions whose ``updated_at`` is
within 120 s of the crash, and ``updated_at`` moves at turn start and end, so
a long turn that died minutes in was never resumed. The in-flight marker is a
``resume_pending`` reason set right before ``run_conversation`` and cleared
right after it; if the process dies in between, startup auto-resume finds it.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.core import TURN_IN_FLIGHT_REASON, resume_reason_phrase
from gateway.session import SessionEntry, SessionSource, SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _store(tmp_path):
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _entry(store):
    return store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="u1"))


def test_marker_is_set_and_cleared_around_a_turn(tmp_path):
    store = _store(tmp_path)
    key = _entry(store).session_key
    token = store.mark_turn_in_flight(key)
    assert token
    e = store._entries[key]
    assert e.resume_pending and e.resume_reason == TURN_IN_FLIGHT_REASON
    assert store.end_turn_in_flight(key, token) is True
    e = store._entries[key]
    assert not e.resume_pending and e.resume_reason is None


def test_marker_survives_a_process_restart(tmp_path):
    store = _store(tmp_path)
    key = _entry(store).session_key
    store.mark_turn_in_flight(key)
    reloaded = _store(tmp_path)
    reloaded._ensure_loaded()
    assert reloaded._entries[key].resume_reason == TURN_IN_FLIGHT_REASON


def test_recovery_turn_keeps_its_original_reason(tmp_path):
    # A turn that is itself resuming a restart must not relabel the session,
    # or a second interruption would lose the original wording.
    store = _store(tmp_path)
    key = _entry(store).session_key
    store.mark_resume_pending(key, "restart_timeout")
    assert store.mark_turn_in_flight(key) is None
    assert store.end_turn_in_flight(key, "any") is False
    assert store._entries[key].resume_reason == "restart_timeout"


def test_shutdown_marker_set_mid_turn_is_not_cleared_by_turn_end(tmp_path):
    # Drain shutdown re-marks running sessions as restart/shutdown_timeout and
    # then interrupts them; the turn ending must leave that marker alone.
    store = _store(tmp_path)
    key = _entry(store).session_key
    token = store.mark_turn_in_flight(key)
    store.mark_resume_pending(key, "shutdown_timeout")
    assert store.end_turn_in_flight(key, token) is False
    e = store._entries[key]
    assert e.resume_pending and e.resume_reason == "shutdown_timeout"


def test_suspended_session_is_never_marked(tmp_path):
    store = _store(tmp_path)
    key = _entry(store).session_key
    store.suspend_session(key)
    assert store.mark_turn_in_flight(key) is None


def test_unknown_session_is_a_no_op(tmp_path):
    store = _store(tmp_path)
    assert store.mark_turn_in_flight("nope") is None
    assert store.end_turn_in_flight("nope", "t") is False


def test_a_stale_worker_cannot_clear_a_newer_turns_marker(tmp_path):
    # After /new the old worker may return while a replacement turn already
    # marked the same session key; only the owning token clears it.
    store = _store(tmp_path)
    key = _entry(store).session_key
    old = store.mark_turn_in_flight(key)
    store.end_turn_in_flight(key, old)
    new = store.mark_turn_in_flight(key)
    assert store.end_turn_in_flight(key, old) is False
    assert store._entries[key].resume_reason == TURN_IN_FLIGHT_REASON
    assert store.end_turn_in_flight(key, new) is True


def test_heartbeat_keeps_a_long_turn_fresh(tmp_path):
    store = _store(tmp_path)
    key = _entry(store).session_key
    token = store.mark_turn_in_flight(key)
    long_ago = datetime.now() - timedelta(hours=3)
    store._entries[key].last_resume_marked_at = long_ago
    assert store.touch_turn_in_flight(key, token) is True
    assert store._entries[key].last_resume_marked_at > long_ago
    assert store.touch_turn_in_flight(key, "someone-else") is False


def test_crash_recovery_note_names_the_exit():
    phrase = resume_reason_phrase(TURN_IN_FLIGHT_REASON)
    assert "exit" in phrase
    assert resume_reason_phrase("restart_timeout") == "a gateway restart"
    assert resume_reason_phrase("shutdown_timeout") == "a gateway shutdown"
    assert resume_reason_phrase(None) == "a gateway restart"


@pytest.mark.asyncio
async def test_startup_auto_resumes_a_turn_that_was_in_flight():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="long-turn")
    entry = SessionEntry(
        session_key="agent:main:telegram:dm:long-turn",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason=TURN_IN_FLIGHT_REASON,
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries = {entry.session_key: entry}
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()
