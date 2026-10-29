"""Gateway /approve and /deny honour requester binding (plan item 2.6)."""
from unittest.mock import patch

import pytest

from tests.gateway.test_approve_deny_commands import (
    _clear_approval_state, _make_event, _make_runner, _make_source,
)


@pytest.fixture(autouse=True)
def _state():
    _clear_approval_state()
    with patch("tools.approval._get_approval_config", return_value={}):
        yield
    _clear_approval_state()


def _pending(runner, requester):
    from tools.approval import _ApprovalEntry, _gateway_queues
    key = runner._session_key_for_source(_make_source())
    entry = _ApprovalEntry({"command": "rm -rf build", "request_id": "r1", "requester": requester})
    _gateway_queues[key] = [entry]
    return entry


@pytest.mark.asyncio
async def test_someone_else_cannot_approve_or_deny_the_request():
    runner = _make_runner()
    entry = _pending(runner, requester="someone-else")
    reply = await runner._handle_approve_command(_make_event("/approve"))
    assert "only" in reply.lower()
    assert not entry.event.is_set()
    reply = await runner._handle_deny_command(_make_event("/deny all"))
    assert "only" in reply.lower()
    assert not entry.event.is_set()


@pytest.mark.asyncio
async def test_the_requester_can_approve():
    runner = _make_runner()
    entry = _pending(runner, requester="u1")      # _make_source() user
    await runner._handle_approve_command(_make_event("/approve"))
    assert entry.event.is_set() and entry.result == "once"
