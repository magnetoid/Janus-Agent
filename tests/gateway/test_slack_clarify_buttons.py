"""Slack renders clarify choices as Block Kit buttons (plan item 3.1)."""
import threading
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_slack_approval_buttons import _attach_auth_runner, _make_adapter
from tools import clarify_gateway


@pytest.fixture(autouse=True)
def _clean():
    clarify_gateway.clear_session("sess-1")
    yield
    clarify_gateway.clear_session("sess-1")


def _register(choices=("Postgres", "SQLite")):
    clarify_gateway.register(clarify_id="cid1", session_key="sess-1",
                             question="Which DB?", choices=list(choices) if choices else None)


async def _send(adapter, choices):
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "111.222"})
    result = await adapter.send_clarify(chat_id="C1", question="Which DB?", choices=choices,
                                        clarify_id="cid1", session_key="sess-1")
    return result, client.chat_postMessage.call_args.kwargs


@pytest.mark.asyncio
async def test_choices_become_one_button_each_plus_other():
    adapter = _make_adapter()
    result, kwargs = await _send(adapter, ["Postgres", "SQLite"])
    assert result.success
    actions = [b for b in kwargs["blocks"] if b["type"] == "actions"][0]["elements"]
    assert [e["action_id"] for e in actions] == ["janus_clarify_0", "janus_clarify_1",
                                                 "janus_clarify_other"]
    assert all(e["value"].startswith("cid1|") for e in actions)
    body = " ".join(b.get("text", {}).get("text", "") for b in kwargs["blocks"])
    assert "Which DB?" in body and "Postgres" in body and "SQLite" in body


@pytest.mark.asyncio
async def test_open_question_is_plain_text_without_buttons():
    adapter = _make_adapter()
    result, kwargs = await _send(adapter, None)
    assert result.success
    assert not any(b["type"] == "actions" for b in kwargs.get("blocks") or [])
    assert "Which DB?" in kwargs["text"]


def _click(adapter, action_id, value, user="U_ALICE"):
    body = {"message": {"ts": "111.222", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "❓ Which DB?"}}]},
            "channel": {"id": "C1"}, "user": {"name": "alice", "id": user}}
    adapter._team_clients["T1"].chat_update = AsyncMock()
    return adapter._handle_clarify_action(AsyncMock(), body,
                                          {"action_id": action_id, "value": value})


@pytest.mark.asyncio
async def test_clicking_a_choice_resolves_the_clarify_with_its_text():
    adapter = _make_adapter()
    _attach_auth_runner(adapter)
    _register()
    await _send(adapter, ["Postgres", "SQLite"])
    got = {}
    waiter = threading.Thread(target=lambda: got.update(r=clarify_gateway.wait_for_response("cid1", 5)))
    waiter.start()
    await _click(adapter, "janus_clarify_1", "cid1|1")
    waiter.join(5)
    assert got["r"] == "SQLite"
    adapter._team_clients["T1"].chat_update.assert_called_once()


@pytest.mark.asyncio
async def test_other_switches_to_typed_answer():
    adapter = _make_adapter()
    _attach_auth_runner(adapter)
    _register()
    await _send(adapter, ["Postgres", "SQLite"])
    await _click(adapter, "janus_clarify_other", "cid1|other")
    entry = clarify_gateway.get_pending_for_session("sess-1")
    assert entry is not None and entry.awaiting_text


@pytest.mark.asyncio
async def test_unauthorized_or_stale_clicks_resolve_nothing():
    adapter = _make_adapter()
    _attach_auth_runner(adapter, auth_fn=lambda source: False)
    _register()
    await _send(adapter, ["Postgres", "SQLite"])
    await _click(adapter, "janus_clarify_0", "cid1|0")
    assert clarify_gateway.has_pending("sess-1")
    adapter._team_clients["T1"].chat_update.assert_not_called()

    authed = _make_adapter()
    _attach_auth_runner(authed)
    await _click(authed, "janus_clarify_0", "gone|0")     # never sent by this adapter
    assert clarify_gateway.has_pending("sess-1")


def test_clarify_actions_are_registered_with_bolt():
    import inspect
    from gateway.platforms import slack
    src = inspect.getsource(slack.SlackAdapter)
    assert "janus_clarify_other" in src and "_handle_clarify_action" in src
