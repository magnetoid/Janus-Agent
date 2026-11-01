"""Gateway ``/agents stop|steer`` is refused: the subagent registry is shared by every chat."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tools import delegate_tool


class _Child:
    def __init__(self):
        self.calls = []

    def interrupt(self, reason):
        self.calls.append(("interrupt", reason))

    def steer(self, text):
        self.calls.append(("steer", text))
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["stop sa-x", "Steer sa-x please stop"])
async def test_gateway_does_not_reach_subagents(args):
    child = _Child()
    delegate_tool._register_subagent({"subagent_id": "sa-x", "depth": 0, "agent": child})
    try:
        runner = object.__new__(GatewayRunner)
        event = MessageEvent(
            text=f"/agents {args}",
            message_type=MessageType.TEXT,
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        )
        reply = await runner._handle_agents_command(event)
    finally:
        delegate_tool._unregister_subagent("sa-x")

    assert child.calls == []
    assert "CLI" in reply and "TUI" in reply
