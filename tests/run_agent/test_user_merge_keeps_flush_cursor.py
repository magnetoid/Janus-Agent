"""Consecutive user messages are merged on the wire, never in the stored history.

A history that ends with an unanswered user message (the previous turn hit a
429, a timeout or an interrupt, and the gateway kept only the question) plus
the new question gives two user messages in a row. The pre-request repair
merged them by shrinking the canonical ``messages`` list *after* the
turn-start flush had advanced the DB cursor, so the assistant's reply landed
below the cursor and was never written to state.db. Gateway sessions reload
history from state.db, so every later turn started with consecutive users
again and lost its reply too.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from janus_state import SessionDB
from run_agent import AIAgent

SID = "sess-user-merge"


def _response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None, reasoning=None)
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model"
    )
    resp.usage = None
    return resp


@pytest.fixture()
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _agent(db, reply: str, requests: list):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=SID,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.save_trajectories = False

    def _create(*_a, **kwargs):
        requests.append([dict(m) for m in kwargs["messages"]])
        return _response(reply)

    agent.client.chat.completions.create.side_effect = _create
    return agent


def _run(agent, text: str, history: list):
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        return agent.run_conversation(text, conversation_history=history)


def _history(db):
    return [
        {"role": m["role"], "content": m.get("content")}
        for m in db.get_messages_as_conversation(SID)
        if m.get("role") in ("user", "assistant")
    ]


def _rows(db):
    return [(r["role"], r.get("content")) for r in db.get_messages(SID)]


def _seed(db):
    seeder = _agent(db, "unused", [])
    seeder._ensure_db_session()
    for role, content in (("user", "q1"), ("assistant", "a1"), ("user", "q2")):
        db.append_message(session_id=SID, role=role, content=content)


def test_reply_after_unanswered_user_turn_is_persisted(db):
    _seed(db)
    requests: list = []
    agent = _agent(db, "THE-REPLY", requests)

    result = _run(agent, "q3", _history(db))

    assert result["final_response"] == "THE-REPLY"
    # Stored history keeps both questions as their own rows, then the reply.
    assert _rows(db) == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("user", "q3"),
        ("assistant", "THE-REPLY"),
    ]
    # The provider still gets strict alternation: one merged user turn.
    users = [m["content"] for m in requests[-1] if m["role"] == "user"]
    assert users == ["q1", "q2\n\nq3"]


def test_next_turn_from_reloaded_history_is_persisted_too(db):
    _seed(db)
    _run(_agent(db, "THE-REPLY", []), "q3", _history(db))

    requests: list = []
    _run(_agent(db, "R4", requests), "q4", _history(db))

    assert _rows(db)[-2:] == [("user", "q4"), ("assistant", "R4")]
    # Earlier questions are not folded into the new one.
    users = [m["content"] for m in requests[-1] if m["role"] == "user"]
    assert users == ["q1", "q2\n\nq3", "q4"]


def test_caller_history_dicts_are_not_mutated(db):
    _seed(db)
    history = _history(db)
    before = [dict(m) for m in history]

    _run(_agent(db, "THE-REPLY", []), "q3", history)

    assert history == before
