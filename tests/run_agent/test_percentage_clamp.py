"""Displayed pressure percentages are clamped at 100% on every render path.

PR #3480 capped the context-pressure percentage in ``agent/display.py`` but
left the same unclamped ``current / limit * 100`` pattern in the other
surfaces. Token/char counts legitimately overshoot their limit (mid-stream,
or before compression fires), so without a clamp users see nonsense like
"126% of context used" in ``/stats``, gateway ``/usage``, and memory-tool
output.

These tests drive the real render paths and assert the *rendered* percentage.
The invariant is "no surface ever displays more than 100%", not "file X
contains the literal ``min(100, ...)``" — the previous version of this file
asserted the latter and broke, with no behavior change whatsoever, when the
gateway command handlers moved out of ``gateway/run.py`` into
``gateway/runner.py``.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Matches "42%" / "99.5%" anywhere in rendered output.
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _percentages(text: str) -> list[float]:
    """Every percentage value rendered in ``text``."""
    return [float(v) for v in _PCT_RE.findall(text)]


def _assert_all_clamped(text: str, *, expect_at_least_one: bool = True) -> list[float]:
    pcts = _percentages(text)
    if expect_at_least_one:
        assert pcts, f"no percentage rendered at all in:\n{text}"
    over = [p for p in pcts if p > 100]
    assert not over, f"unclamped percentage(s) {over} rendered in:\n{text}"
    return pcts


# ── tools/memory_tool.py — MemoryStore usage indicators ────────────────────


class TestMemoryStorePercentClamp:
    """``_success_response`` and ``_render_block`` render a usage percentage."""

    @staticmethod
    def _store(entries, *, limit=200):
        from tools.memory_tool import MemoryStore

        store = MemoryStore(memory_char_limit=limit, user_char_limit=limit)
        store.memory_entries = list(entries)
        return store

    def test_success_response_clamped_when_over_limit(self):
        # 500 chars against a 200-char limit — 250% unclamped.
        store = self._store(["x" * 500])
        usage = store._success_response("memory")["usage"]
        pcts = _assert_all_clamped(usage)
        assert pcts[0] == 100, f"expected the clamped ceiling, got {usage!r}"

    def test_render_block_clamped_when_over_limit(self):
        entries = ["y" * 500]
        store = self._store(entries)
        block = store._render_block("memory", entries)
        pcts = _assert_all_clamped(block)
        assert pcts[0] == 100, f"expected the clamped ceiling, got {block!r}"

    def test_under_limit_percentage_is_still_proportional(self):
        """The clamp must not flatten normal values to a constant."""
        store = self._store(["z" * 100], limit=200)
        usage = store._success_response("memory")["usage"]
        assert _percentages(usage) == [50.0], usage

    def test_zero_limit_renders_zero_not_a_crash(self):
        store = self._store(["z" * 100], limit=0)
        usage = store._success_response("memory")["usage"]
        assert _percentages(usage) == [0.0], usage


# ── cli.py — /stats + /usage session report ────────────────────────────────


def _cli_with_agent(*, context_tokens: int, context_length: int):
    from cli import JanusCLI

    cli_obj = JanusCLI.__new__(JanusCLI)
    cli_obj.model = "local/test-model"
    cli_obj.session_start = datetime.now() - timedelta(minutes=3)
    cli_obj.conversation_history = [{"role": "user", "content": "hi"}]
    cli_obj.verbose = False
    cli_obj.agent = SimpleNamespace(
        model=cli_obj.model,
        # provider=None keeps the account-usage portal fetch out of the path.
        provider=None,
        base_url="",
        api_key=None,
        session_input_tokens=context_tokens,
        session_output_tokens=1_000,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_prompt_tokens=context_tokens,
        session_completion_tokens=1_000,
        session_total_tokens=context_tokens + 1_000,
        session_api_calls=3,
        get_rate_limit_state=lambda: None,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=context_tokens,
            context_length=context_length,
            compression_count=0,
        ),
    )
    return cli_obj


class TestCLIUsagePercentClamp:
    """``JanusCLI._show_usage`` prints "Current context: X / Y (Z%)"."""

    def _render(self, cli_obj, capsys) -> str:
        # The Nous credits block is an independent portal fetch with its own
        # percentage gauge — silence it so the assertion stays scoped to the
        # context-pressure line.
        with patch.object(type(cli_obj), "_print_nous_credits_block", lambda self: False):
            cli_obj._show_usage()
        return capsys.readouterr().out

    def test_over_context_clamped_at_100(self, capsys):
        cli_obj = _cli_with_agent(context_tokens=210_000, context_length=200_000)
        out = self._render(cli_obj, capsys)
        pcts = _assert_all_clamped(out)
        assert max(pcts) == 100, out

    def test_under_context_percentage_is_still_proportional(self, capsys):
        cli_obj = _cli_with_agent(context_tokens=100_000, context_length=200_000)
        out = self._render(cli_obj, capsys)
        assert _percentages(out) == [50.0], out

    def test_zero_context_length_renders_zero_not_a_crash(self, capsys):
        cli_obj = _cli_with_agent(context_tokens=1_000, context_length=0)
        out = self._render(cli_obj, capsys)
        assert _percentages(out) == [0.0], out


# ── gateway — /usage session report ────────────────────────────────────────


def _gateway_agent(*, context_tokens: int, context_length: int):
    agent = MagicMock()
    for key, value in {
        "model": "local/test-model",
        "provider": None,
        "base_url": None,
        "api_key": None,
        "session_total_tokens": context_tokens + 1_000,
        "session_api_calls": 3,
        "session_prompt_tokens": context_tokens,
        "session_completion_tokens": 1_000,
        "session_input_tokens": context_tokens,
        "session_output_tokens": 1_000,
        "session_cache_read_tokens": 0,
        "session_cache_write_tokens": 0,
    }.items():
        setattr(agent, key, value)
    agent.get_rate_limit_state.return_value = None
    agent.context_compressor = SimpleNamespace(
        last_prompt_tokens=context_tokens,
        context_length=context_length,
        compression_count=0,
    )
    return agent


def _gateway_runner(agent):
    from gateway.run import GatewayRunner

    session_key = "agent:main:telegram:private:12345"
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {session_key: agent}
    runner._running_agents_ts = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = None
    runner.session_store = MagicMock()
    runner._session_key_for_source = MagicMock(return_value=session_key)
    return runner


async def _render_gateway_usage(agent) -> str:
    runner = _gateway_runner(agent)
    with patch("agent.account_usage.nous_credits_lines", return_value=[]):
        return await runner._handle_usage_command(MagicMock())


class TestGatewayUsagePercentClamp:
    @pytest.mark.asyncio
    async def test_over_context_clamped_at_100(self):
        out = await _render_gateway_usage(
            _gateway_agent(context_tokens=210_000, context_length=200_000)
        )
        pcts = _assert_all_clamped(out)
        assert max(pcts) == 100, out

    @pytest.mark.asyncio
    async def test_under_context_percentage_is_still_proportional(self):
        out = await _render_gateway_usage(
            _gateway_agent(context_tokens=150_000, context_length=200_000)
        )
        assert _percentages(out) == [75.0], out

    @pytest.mark.asyncio
    async def test_zero_context_length_renders_zero_not_a_crash(self):
        out = await _render_gateway_usage(
            _gateway_agent(context_tokens=1_000, context_length=0)
        )
        assert _percentages(out) == [0.0], out
