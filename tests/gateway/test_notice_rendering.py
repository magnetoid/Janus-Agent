"""Unit tests for messaging-gateway notice rendering.

Covers render_notice_line — the pure helper that turns an AgentNotice into the
single plaintext line pushed standalone over a messaging platform (no status
bar, unlike the TUI). Behavior contracts, not data snapshots.
"""
from agent.notices import AgentNotice
from gateway.run import render_notice_line


class TestRenderNoticeLine:
    """render_notice_line emits the notice text VERBATIM.

    The notice policy already bakes the level glyph (⚠ / • / ✕ / ✓) into the
    text, and the TUI + CLI REPL render it as-is — so messaging must NOT add a
    second glyph, which would double it ("⚠ ⚠ Credits 90% used", "⛔ ✕ Credit
    access paused").
    """

    def test_returns_text_verbatim_with_its_baked_glyph(self):
        assert (
            render_notice_line(AgentNotice(text="⚠ Credits 90% used · $20.00 cap", level="warn"))
            == "⚠ Credits 90% used · $20.00 cap"
        )
        assert (
            render_notice_line(AgentNotice(text="• Grant spent · $5.00 top-up left", level="info"))
            == "• Grant spent · $5.00 top-up left"
        )
        assert (
            render_notice_line(
                AgentNotice(text="✕ Credit access paused · run /usage for balance", level="error")
            )
            == "✕ Credit access paused · run /usage for balance"
        )

    def test_does_not_prepend_a_second_glyph(self):
        # Regression: the text already carries its glyph; the level must not add
        # another (the bug produced "⚠ ⚠ …" / "⛔ ✕ …").
        line = render_notice_line(AgentNotice(text="⚠ Credits 90% used", level="warn"))
        assert line == "⚠ Credits 90% used"
        assert "⚠ ⚠" not in line

    def test_text_is_stripped(self):
        assert render_notice_line(AgentNotice(text="  ⚠ padded  ", level="warn")) == "⚠ padded"

    def test_empty_text_returns_empty_string(self):
        # Empty/whitespace → "" → the callback suppresses the push. Fail-soft.
        assert render_notice_line(AgentNotice(text="", level="warn")) == ""
        assert render_notice_line(AgentNotice(text="   ", level="warn")) == ""

    def test_malformed_notice_does_not_raise(self):
        # Duck-typed: a stand-in lacking the expected attrs degrades to "".
        class _Bare:
            pass

        assert render_notice_line(_Bare()) == ""


# ── Delivery seam: a rendered notice line goes out via _deliver_platform_notice ──

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_source(platform_value="telegram", chat_id="555", user_id="u1"):
    src = MagicMock()
    plat = MagicMock()
    plat.value = platform_value
    src.platform = plat
    src.chat_id = chat_id
    src.user_id = user_id
    return src


def _make_runner_with_adapter(source, adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {source.platform: adapter}
    runner.config = MagicMock()
    runner.config.get_notice_delivery = MagicMock(return_value="public")
    runner._thread_metadata_for_source = MagicMock(return_value={"thread": "t"})
    return runner


class TestDeliverNoticeLine:
    """The seam between render_notice_line and the platform adapter.

    Proves a rendered credit-notice line reaches adapter.send (public) /
    send_private_notice (private) through the shared _deliver_platform_notice
    rail — the path the gateway notice_callback schedules onto the loop.
    """

    @pytest.mark.asyncio
    async def test_public_delivery_sends_rendered_line(self):
        source = _make_source()
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True))
        runner = _make_runner_with_adapter(source, adapter)

        line = render_notice_line(
            AgentNotice(text="⚠ Credits 90% used · $20.00 cap", level="warn")
        )
        await runner._deliver_platform_notice(source, line)

        adapter.send.assert_awaited_once()
        args, kwargs = adapter.send.call_args
        assert args[0] == "555"
        # Delivered verbatim — the policy's single glyph, not a doubled one.
        assert args[1] == "⚠ Credits 90% used · $20.00 cap"

    @pytest.mark.asyncio
    async def test_private_delivery_prefers_private_notice(self):
        source = _make_source()
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True))
        adapter.send_private_notice = AsyncMock(return_value=MagicMock(success=True))
        runner = _make_runner_with_adapter(source, adapter)
        runner.config.get_notice_delivery = MagicMock(return_value="private")

        line = render_notice_line(
            AgentNotice(text="✓ Credit access restored", level="success")
        )
        await runner._deliver_platform_notice(source, line)

        adapter.send_private_notice.assert_awaited_once()
        adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_adapter_is_a_noop(self):
        source = _make_source()
        runner = object.__new__(__import__("gateway.run", fromlist=["GatewayRunner"]).GatewayRunner)
        runner.adapters = {}
        # Must not raise when the platform has no registered adapter.
        await runner._deliver_platform_notice(source, "• anything")

