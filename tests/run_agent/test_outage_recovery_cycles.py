"""agent.auto_recovery_cycles: wait out a provider outage instead of ending the turn.

When every retry (and every fallback) fails with an outage-shaped error —
503/529 overload, 5xx, timeout — the turn used to end with "API call failed
after N retries". With ``auto_recovery_cycles: N`` the loop waits, says so,
and runs the whole retry block again, up to N times. Errors that waiting
cannot fix (bad request, auth, billing) still end the turn at once.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import conversation_loop as _conv_loop
from run_agent import AIAgent


class _ProviderError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def _overloaded():
    return _ProviderError(503, "Service Unavailable: upstream overloaded")


def _ok():
    msg = SimpleNamespace(content="Back online", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="test/model", usage=None)


def _agent(cycles=None):
    cfg = {"agent": {"api_max_retries": 2}}
    if cycles is not None:
        cfg["agent"]["auto_recovery_cycles"] = cycles
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("janus_cli.config.load_config", return_value=cfg),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.compression_enabled = False
    a.save_trajectories = False
    return a


def _fast_time():
    t = [1000.0]

    def now():
        t[0] += 500.0
        return t[0]

    mock = MagicMock()
    mock.time.side_effect = now
    mock.sleep = MagicMock()
    mock.monotonic.return_value = 12345.0
    return mock


def _run(agent, responses):
    agent.client.chat.completions.create.side_effect = responses
    statuses = []
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_emit_status", side_effect=statuses.append),
        patch("run_agent.time", _fast_time()),
        patch.object(_conv_loop, "time", _fast_time()),
        patch.object(_conv_loop, "jittered_backoff", lambda *a, **k: 0.0),
    ):
        result = agent.run_conversation("hello")
    return result, statuses


# Two attempts per retry block: the first block fails, the provider comes
# back during the first recovery cycle.
OUTAGE_THEN_OK = [_overloaded()] * 3 + [_ok()]


class TestConfig:
    def test_off_by_default(self):
        assert _agent()._auto_recovery_cycles == 0

    @pytest.mark.parametrize("raw,expected", [(3, 3), ("2", 2), (-1, 0), ("lots", 0), (None, 0)])
    def test_parsed_and_clamped(self, raw, expected):
        assert _agent(raw)._auto_recovery_cycles == expected


class TestRecovery:
    def test_turn_survives_an_outage_within_the_cycle_budget(self):
        agent = _agent(cycles=1)

        result, statuses = _run(agent, OUTAGE_THEN_OK)

        assert result.get("completed") is True, result
        assert result["final_response"] == "Back online"
        assert any("recovery cycle 1/1" in s for s in statuses), statuses

    def test_gives_up_when_the_cycles_run_out(self):
        agent = _agent(cycles=1)

        result, _ = _run(agent, [_overloaded()] * 6 + [_ok()])

        assert result.get("failed") is True
        assert agent.client.chat.completions.create.call_count == 4

    def test_default_ends_the_turn_like_before(self):
        agent = _agent()

        result, statuses = _run(agent, OUTAGE_THEN_OK)

        assert result.get("failed") is True
        assert agent.client.chat.completions.create.call_count == 2
        assert not any("recovery cycle" in s for s in statuses)

    def test_errors_waiting_cannot_fix_end_the_turn(self):
        agent = _agent(cycles=3)
        bad_request = _ProviderError(400, "Invalid request: unknown parameter 'foo'")

        result, statuses = _run(agent, [bad_request] * 10)

        assert result.get("failed") is True
        assert not any("recovery cycle" in s for s in statuses)

    def test_rate_limits_are_not_an_outage(self):
        agent = _agent(cycles=3)
        throttled = _ProviderError(429, "Too Many Requests")

        result, statuses = _run(agent, [throttled] * 10)

        assert result.get("failed") is True
        assert agent.client.chat.completions.create.call_count < 10
        assert not any("recovery cycle" in s for s in statuses)

    def test_interrupt_during_the_wait_stops_the_turn(self):
        agent = _agent(cycles=2)
        real_emit = []

        def interrupt_on_cycle(msg):
            real_emit.append(msg)
            if "recovery cycle" in msg:
                agent._interrupt_requested = True

        agent.client.chat.completions.create.side_effect = OUTAGE_THEN_OK
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_emit_status", side_effect=interrupt_on_cycle),
            patch("run_agent.time", _fast_time()),
            patch.object(_conv_loop, "time", _fast_time()),
            patch.object(_conv_loop, "jittered_backoff", lambda *a, **k: 0.0),
        ):
            result = agent.run_conversation("hello")

        assert result.get("interrupted") is True, result
        assert agent.client.chat.completions.create.call_count == 2


class TestWaitSchedule:
    def test_waits_grow_and_are_capped(self):
        waits = [_conv_loop._recovery_cycle_wait(n) for n in range(1, 8)]

        assert waits == sorted(waits)
        assert waits[0] > 0
        assert max(waits) <= 300

    def test_an_interrupt_cuts_the_wait_short(self):
        clock = [0.0]
        agent = SimpleNamespace(_interrupt_requested=False, _touch_activity=lambda msg: None)

        def sleep(seconds):
            clock[0] += seconds
            agent._interrupt_requested = True  # user presses Ctrl+C mid-wait

        fake_time = SimpleNamespace(time=lambda: clock[0], sleep=sleep)
        with patch.object(_conv_loop, "time", fake_time):
            finished = _conv_loop._wait_for_recovery_cycle(agent, 300, "cycle 1/1")

        assert finished is False
        assert clock[0] < 1  # stopped within one tick, not after 300s
