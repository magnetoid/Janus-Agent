"""Tests that on_session_finalize and on_session_reset plugin hooks fire in the gateway."""
import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig, SessionResetPolicy
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    new_session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = new_session_entry
    runner.session_store.reset_session.return_value = new_session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
@patch("janus_cli.plugins.invoke_hook")
async def test_reset_fires_finalize_hook(mock_invoke_hook):
    """/new must fire on_session_finalize with the OLD session id."""
    runner = _make_runner()

    await runner._handle_reset_command(_make_event("/new"))

    assert any(
        c.args == ("on_session_finalize",)
        and c.kwargs["session_id"] == "sess-old"
        and c.kwargs["platform"] == "telegram"
        and c.kwargs["old_session_id"] == "sess-old"
        and c.kwargs["new_session_id"] == "sess-new"
        for c in mock_invoke_hook.call_args_list
    )


@pytest.mark.asyncio
@patch("janus_cli.plugins.invoke_hook")
async def test_reset_fires_reset_hook(mock_invoke_hook):
    """/new must fire on_session_reset with the NEW session id."""
    runner = _make_runner()

    await runner._handle_reset_command(_make_event("/new"))

    assert any(
        c.args == ("on_session_reset",)
        and c.kwargs["session_id"] == "sess-new"
        and c.kwargs["platform"] == "telegram"
        and c.kwargs["old_session_id"] == "sess-old"
        and c.kwargs["new_session_id"] == "sess-new"
        for c in mock_invoke_hook.call_args_list
    )


@pytest.mark.asyncio
@patch("janus_cli.plugins.invoke_hook")
async def test_finalize_before_reset(mock_invoke_hook):
    """on_session_finalize must fire before on_session_reset."""
    runner = _make_runner()

    await runner._handle_reset_command(_make_event("/new"))

    calls = [c for c in mock_invoke_hook.call_args_list
             if c[0][0] in {"on_session_finalize", "on_session_reset"}]
    hook_names = [c[0][0] for c in calls]
    assert hook_names == ["on_session_finalize", "on_session_reset"]


@pytest.mark.asyncio
@patch("janus_cli.plugins.invoke_hook")
async def test_shutdown_fires_finalize_for_active_agents(mock_invoke_hook):
    """Gateway stop() must fire on_session_finalize for each active agent."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._background_tasks = set()
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._shutdown_event = MagicMock()
    runner.adapters = {}
    runner._exit_reason = "test"
    runner._exit_code = None
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._running_agents_ts = {}
    runner._update_runtime_status = MagicMock()

    agent1 = MagicMock()
    agent1.session_id = "sess-a"
    agent2 = MagicMock()
    agent2.session_id = "sess-b"
    runner._running_agents = {"key-a": agent1, "key-b": agent2}

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        await runner.stop()

    finalize_calls = [
        c for c in mock_invoke_hook.call_args_list
        if c[0][0] == "on_session_finalize"
    ]
    session_ids = {c[1]["session_id"] for c in finalize_calls}
    assert session_ids == {"sess-a", "sess-b"}


@pytest.mark.asyncio
@patch("janus_cli.plugins.invoke_hook", side_effect=Exception("boom"))
async def test_hook_error_does_not_break_reset(mock_invoke_hook):
    """Plugin hook errors must not prevent /new from completing."""
    runner = _make_runner()

    result = await runner._handle_reset_command(_make_event("/new"))

    # Should still return a success message despite hook errors
    assert "Session reset" in result or "New session" in result


@pytest.mark.asyncio
@patch("janus_cli.plugins.invoke_hook")
async def test_idle_expiry_fires_finalize_hook(mock_invoke_hook):
    """Regression test for #14981.

    When ``_session_expiry_watcher`` sweeps a session that has aged past
    its reset policy (idle timeout, scheduled reset), it must fire
    ``on_session_finalize`` so plugin providers get the same final-pass
    extraction opportunity they'd get from /new or CLI shutdown.  Before
    the fix, the expiry path evicted the agent but silently skipped the
    hook.
    """
    from datetime import datetime, timedelta

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._last_session_store_prune_ts = 0.0

    session_key = "agent:main:telegram:dm:42"
    expired_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-expired",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=2),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    expired_entry.expiry_finalized = False

    runner.session_store = MagicMock()
    runner.session_store._ensure_loaded = MagicMock()
    runner.session_store._entries = {session_key: expired_entry}
    runner.session_store._is_session_expired = MagicMock(return_value=True)
    runner.session_store._lock = MagicMock()
    runner.session_store._lock.__enter__ = MagicMock(return_value=None)
    runner.session_store._lock.__exit__ = MagicMock(return_value=None)
    runner.session_store._save = MagicMock()

    runner._evict_cached_agent = MagicMock()
    runner._cleanup_agent_resources = MagicMock()
    runner._sweep_idle_cached_agents = MagicMock(return_value=0)

    # The watcher starts with `await asyncio.sleep(60)` and loops while
    # `self._running`.  Patch sleep so the 60s initial delay is instant, and
    # make the expiry hook invocation flip `_running` false so the loop
    # exits cleanly after one pass.
    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    def _hook_and_stop(*a, **kw):
        runner._running = False
        return None

    mock_invoke_hook.side_effect = _hook_and_stop

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await runner._session_expiry_watcher(interval=0)

    # Look for the finalize call targeting the expired session.
    finalize_calls = [
        c for c in mock_invoke_hook.call_args_list
        if c[0] and c[0][0] == "on_session_finalize"
    ]
    session_ids = {c[1].get("session_id") for c in finalize_calls}
    assert "sess-expired" in session_ids, (
        f"on_session_finalize was not fired during idle expiry; "
        f"got session_ids={session_ids} (regression of #14981)"
    )


# ---------------------------------------------------------------------------
# The expiry watcher and turns that are still running
# ---------------------------------------------------------------------------
#
# The watcher finalizes sessions whose reset policy says they expired, and
# hard-closes their cached AIAgent (memory provider shutdown, terminal,
# browser, subagents).  The policy reads ``updated_at``, which a turn only
# writes when it starts and after it ends, so a long turn that crosses the
# daily reset hour looks expired while it is still running.

_DAY = datetime(2026, 3, 10)


def _at(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    return _DAY.replace(hour=hour, minute=minute) + timedelta(days=days)


def _expiry_entry(key: str, session_id: str, updated_at: datetime) -> SessionEntry:
    return SessionEntry(
        session_key=key,
        session_id=session_id,
        created_at=updated_at,
        updated_at=updated_at,
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )


def _make_expiry_runner(tmp_path, *entries):
    """A runner whose expiry watcher works against a real SessionStore.

    The reset policy is spelled out (daily at 04:00) rather than taken from
    the defaults, so these tests do not depend on them.
    """
    from gateway.run import GatewayRunner

    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="daily", at_hour=4),
    )
    store = SessionStore(sessions_dir=tmp_path, config=config)
    if store._db is not None:
        store._db.close()
    store._db = None
    store._loaded = True
    for entry in entries:
        store._entries[entry.session_key] = entry

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._running = False
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._last_session_store_prune_ts = float("inf")  # no prune pass

    def _stop_after_this_pass():
        runner._running = False
        return 0

    # The idle sweep runs at the end of every pass, so it ends the loop.
    runner._sweep_idle_cached_agents = MagicMock(side_effect=_stop_after_this_pass)
    return runner


async def _watcher_pass(runner, now: datetime) -> list:
    """Run one pass of the expiry watcher with the clock at ``now``.

    Returns the session_ids that ``on_session_finalize`` fired for.
    """
    runner._running = True
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        await real_sleep(0)

    with patch("gateway.session._now", return_value=now), \
            patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep), \
            patch("janus_cli.plugins.invoke_hook") as hook:
        await asyncio.wait_for(
            runner._session_expiry_watcher(interval=1), timeout=10,
        )
    return [
        c.kwargs.get("session_id")
        for c in hook.call_args_list
        if c.args and c.args[0] == "on_session_finalize"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_slot", ["agent", "pending_sentinel"])
async def test_expiry_watcher_leaves_a_running_turn_alone(tmp_path, turn_slot):
    """A turn that crosses the reset hour keeps its agent and its session.

    The turn started at 03:55, so at the 04:02 tick the policy says the
    session expired.  But the turn is still running: either its live agent
    is registered, or the pending placeholder that claims the session while
    the agent is being built (that turn may already hold the cached agent).
    The watcher must not close or evict the agent, fire
    ``on_session_finalize``, or mark the session finalized.
    """
    from gateway.run import _AGENT_PENDING_SENTINEL

    key = "agent:main:telegram:dm:42"
    entry = _expiry_entry(key, "sess-long-turn", _at(3, 55))
    runner = _make_expiry_runner(tmp_path, entry)
    agent = MagicMock(name="agent")
    runner._agent_cache[key] = (agent, "sig")
    runner._running_agents[key] = (
        agent if turn_slot == "agent" else _AGENT_PENDING_SENTINEL
    )
    with patch("gateway.session._now", return_value=_at(4, 2)):
        assert runner.session_store._is_session_expired(entry)  # precondition

    finalized = await _watcher_pass(runner, _at(4, 2))

    agent.shutdown_memory_provider.assert_not_called()
    agent.close.assert_not_called()
    assert runner._agent_cache[key][0] is agent
    assert "sess-long-turn" not in finalized
    assert entry.expiry_finalized is False


@pytest.mark.asyncio
async def test_expiry_watcher_finalizes_a_spared_session_once_its_turn_ends(tmp_path):
    """Sparing a running turn postpones finalization; it never cancels it.

    Here the turn ends without recording new activity (it errored before
    ``update_session`` ran), so the session is still expired at the next
    tick and must be finalized then, like any idle session.
    """
    key = "agent:main:telegram:dm:42"
    entry = _expiry_entry(key, "sess-long-turn", _at(3, 55))
    runner = _make_expiry_runner(tmp_path, entry)
    agent = MagicMock(name="agent")
    runner._agent_cache[key] = (agent, "sig")
    runner._running_agents[key] = agent

    assert "sess-long-turn" not in await _watcher_pass(runner, _at(4, 2))

    runner._running_agents.pop(key)  # the turn ends
    finalized = await _watcher_pass(runner, _at(4, 7))

    assert "sess-long-turn" in finalized
    agent.shutdown_memory_provider.assert_called_once()
    agent.close.assert_called_once()
    assert key not in runner._agent_cache
    assert entry.expiry_finalized is True


@pytest.mark.asyncio
async def test_expiry_watcher_finalizes_a_turn_that_crossed_reset_at_its_next_expiry(tmp_path):
    """The usual case: the long turn completes and records its activity.

    ``update_session`` moves ``updated_at`` past 04:00, so the session is
    live again (the same verdict ``_should_reset`` gives the next message)
    and the watcher leaves it alone until it really expires at the next
    day's reset.  It must be finalized then: a session wrongly marked
    finalized while its turn ran would be skipped from then on.
    """
    key = "agent:main:telegram:dm:42"
    entry = _expiry_entry(key, "sess-long-turn", _at(3, 55))
    runner = _make_expiry_runner(tmp_path, entry)
    agent = MagicMock(name="agent")
    runner._agent_cache[key] = (agent, "sig")
    runner._running_agents[key] = agent

    assert "sess-long-turn" not in await _watcher_pass(runner, _at(4, 2))

    # The turn completes at 04:10 and records its activity.
    with patch("gateway.session._now", return_value=_at(4, 10)):
        runner.session_store.update_session(key)
    runner._running_agents.pop(key)

    assert "sess-long-turn" not in await _watcher_pass(runner, _at(4, 15))
    agent.close.assert_not_called()

    finalized = await _watcher_pass(runner, _at(4, 2, days=1))

    assert "sess-long-turn" in finalized
    agent.close.assert_called_once()
    assert key not in runner._agent_cache
    assert entry.expiry_finalized is True


@pytest.mark.asyncio
async def test_expiry_watcher_still_resets_idle_sessions_while_another_turn_runs(tmp_path):
    """Only a session with a running turn is spared, not the whole pass."""
    idle_key = "agent:main:telegram:dm:1"
    busy_key = "agent:main:telegram:dm:2"
    idle_entry = _expiry_entry(idle_key, "sess-idle", _at(22, 0, days=-1))
    busy_entry = _expiry_entry(busy_key, "sess-busy", _at(4, 1))
    runner = _make_expiry_runner(tmp_path, idle_entry, busy_entry)
    idle_agent = MagicMock(name="idle_agent")
    busy_agent = MagicMock(name="busy_agent")
    runner._agent_cache[idle_key] = (idle_agent, "sig")
    runner._agent_cache[busy_key] = (busy_agent, "sig")
    runner._running_agents[busy_key] = busy_agent

    finalized = await _watcher_pass(runner, _at(4, 2))

    assert "sess-idle" in finalized
    idle_agent.shutdown_memory_provider.assert_called_once()
    idle_agent.close.assert_called_once()
    assert idle_key not in runner._agent_cache
    assert idle_entry.expiry_finalized is True
    busy_agent.close.assert_not_called()
    assert runner._agent_cache[busy_key][0] is busy_agent
