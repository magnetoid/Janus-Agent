"""``persist_user_message``: API-only content goes to the model, not the transcript.

Callers pass ``persist_user_message`` when the turn's user message carries
content meant only for the model: ACP image and file attachments, Telegram
observed group context, the CLI voice-mode prefix. The model must get that
content on every request of the turn. The session DB and the returned
history (which the CLI and ACP replay as the next turn's history) must hold
the clean text, and must agree with each other, so a turn replays the same
bytes whether its history comes from memory or from the DB.

The turn-start crash-resilience save used to write the override into the
live message before the first API call, so the model only ever saw the clean
text. These tests drive the real loop with the real ``_persist_session`` and
a real SessionDB, and look at what the client was sent.
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from janus_state import SessionDB
from run_agent import AIAgent

VOICE_PREFIX = (
    "[Voice input — respond concisely and conversationally, "
    "2-3 sentences max. No code blocks or markdown.] "
)


def _tool_call(call_id: str):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="probe_tool", arguments="{}"),
    )


def _response(content=None, tool_calls=None, finish_reason="stop"):
    msg = SimpleNamespace(content=content, reasoning=None, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No network: stub model metadata lookups, refuse any socket connect."""
    def _refuse(*_a, **_k):
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata",
                        lambda *a, **k: {})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length",
                        lambda *a, **k: 200_000)
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata",
                        lambda *a, **k: {})
    monkeypatch.setattr("agent.context_compressor.get_model_context_length",
                        lambda *a, **k: 200_000)


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _make_agent(session_db) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions",
              return_value=[{"function": {"name": "probe_tool"}}]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("janus_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            max_iterations=10,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id="persist-override-session",
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = []
    return agent


def _run_turn(agent, user_message, *, persist, n_tool_iterations=1,
              conversation_history=None):
    """One turn: ``n_tool_iterations`` tool rounds, then a final answer.

    Returns the result and the ``messages`` list of every request sent.
    ``_persist_session`` is deliberately NOT stubbed.
    """
    agent.client.chat.completions.create.side_effect = (
        [_response(tool_calls=[_tool_call(f"call_{i}")], finish_reason="tool_calls")
         for i in range(n_tool_iterations)]
        + [_response(content="final answer")]
    )
    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"ok": True})),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            user_message,
            conversation_history=conversation_history,
            persist_user_message=persist,
        )
    requests = [
        c.kwargs["messages"]
        for c in agent.client.chat.completions.create.call_args_list
    ]
    agent.client.chat.completions.create.reset_mock(side_effect=True)
    return result, requests


def _user_contents(messages):
    return [m.get("content") for m in messages if m.get("role") == "user"]


def _text_of(content) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", "")) for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return content or ""


def _assert_no_private_keys(messages):
    leaked = sorted({k for m in messages for k in m if str(k).startswith("_")})
    assert not leaked, f"internal message keys escaped: {leaked}"


def test_api_only_prefix_reaches_every_request_and_db_keeps_clean_text(db):
    agent = _make_agent(db)

    result, requests = _run_turn(
        agent, VOICE_PREFIX + "Hello there", persist="Hello there",
        n_tool_iterations=2,
    )

    assert len(requests) == 3
    sent = [_user_contents(req)[-1] for req in requests]
    # The model hears the voice instruction on every call of the turn, and the
    # message's bytes do not move between calls (prompt-cache prefix).
    assert sent[0].startswith(VOICE_PREFIX)
    assert "Hello there" in sent[0]
    assert all(s == sent[0] for s in sent)
    for req in requests:
        _assert_no_private_keys(req)

    # The transcript gets only the clean text, in the DB and in the returned
    # history, which the CLI replays as the next turn's history.
    db_users = _user_contents(db.get_messages(agent.session_id))
    assert db_users == ["Hello there"]
    assert _user_contents(result["messages"]) == db_users
    _assert_no_private_keys(result["messages"])


def test_next_turn_replays_the_text_the_db_holds(db):
    """A later turn replays the clean text from memory (CLI, ACP) and from the
    DB (gateway) alike, so both sources send the same bytes for that turn."""
    agent = _make_agent(db)
    result1, _ = _run_turn(
        agent, VOICE_PREFIX + "Hello there", persist="Hello there",
        n_tool_iterations=0,
    )

    _, requests2 = _run_turn(
        agent, "and then?", persist=None, n_tool_iterations=1,
        conversation_history=result1["messages"],
    )

    replayed = _user_contents(requests2[0])
    db_users = _user_contents(db.get_messages(agent.session_id))
    assert replayed == db_users == ["Hello there", "and then?"]
    assert all(_user_contents(req) == replayed for req in requests2)


def test_image_attachment_reaches_the_model_and_db_keeps_the_typed_text(db):
    """ACP sends attachments as content parts and persists the typed text.
    On a vision-capable model the image parts go to the provider as they are."""
    agent = _make_agent(db)
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    content = [{"type": "text", "text": "what is this"}, image]

    with patch.object(agent, "_model_supports_vision", return_value=True):
        result, requests = _run_turn(
            agent, content, persist="what is this", n_tool_iterations=1,
        )

    for req in requests:
        sent = _user_contents(req)[-1]
        assert isinstance(sent, list)
        assert image in sent
    assert _user_contents(db.get_messages(agent.session_id)) == ["what is this"]
    assert _user_contents(result["messages"]) == ["what is this"]


def test_compression_mid_turn_keeps_the_override_with_its_message(db):
    """In-loop compression copies every message, moves the current turn's
    message to a new index, and here merges the summary into it (the head ends
    with an assistant turn and the tail starts with this user message). The
    model must still get the API-only prefix; the new session's rows and the
    returned history must get the summary plus the clean text, and no other
    message may be rewritten."""
    agent = _make_agent(db)
    agent.compression_enabled = True
    compressor = agent.context_compressor
    compressor.protect_first_n = 2
    compressor.protect_last_n = 1
    compressor.tail_token_budget = 1
    summary = MagicMock()
    summary.choices = [MagicMock()]
    summary.choices[0].message.content = "EARLIER-WORK-SUMMARY"
    compress_now = iter([True])

    history = []
    for i in range(6):
        history += [{"role": "user", "content": f"old question {i}"},
                    {"role": "assistant", "content": f"old answer {i}"}]

    with (
        # Skip preflight; compress once, after the first tool round.
        patch.object(compressor, "should_defer_preflight_to_real_usage",
                     return_value=True),
        patch.object(compressor, "should_compress",
                     side_effect=lambda *_a, **_k: next(compress_now, False)),
        patch("agent.context_compressor.call_llm", return_value=summary),
    ):
        result, requests = _run_turn(
            agent, VOICE_PREFIX + "Hello there", persist="Hello there",
            n_tool_iterations=1, conversation_history=history,
        )

    assert agent.session_id != "persist-override-session", "compression did not run"
    assert len(requests) == 2
    for req in requests:
        assert VOICE_PREFIX in _text_of(_user_contents(req)[-1])
    assert "EARLIER-WORK-SUMMARY" in _text_of(_user_contents(requests[-1])[-1])

    rows = _user_contents(db.get_messages(agent.session_id))
    returned = _user_contents(result["messages"])
    assert rows == returned
    assert not any(VOICE_PREFIX in _text_of(c) for c in returned)
    current = [c for c in returned if "Hello there" in _text_of(c)]
    assert len(current) == 1
    assert "EARLIER-WORK-SUMMARY" in _text_of(current[0])
    # The protected head comes through untouched.
    assert returned[0] == "old question 0"
    _assert_no_private_keys(result["messages"])


class _SafetyBlock(Exception):
    status_code = 400

    def __init__(self):
        super().__init__("Your request was flagged by our safety system.")


def test_failed_large_session_still_returns_clean_history(db):
    """A 400 on a large session skips the save (#1630) and returns early; the
    history it hands back must still hold the clean text."""
    agent = _make_agent(db)
    history = []
    for i in range(45):
        history += [{"role": "user", "content": f"q{i}"},
                    {"role": "assistant", "content": f"a{i}"}]
    agent.client.chat.completions.create.side_effect = _SafetyBlock()

    with (
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            VOICE_PREFIX + "Hello there",
            conversation_history=history,
            persist_user_message="Hello there",
        )

    assert result["failed"] is True
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert _user_contents(sent)[-1].startswith(VOICE_PREFIX)
    assert _user_contents(result["messages"])[-1] == "Hello there"
    _assert_no_private_keys(result["messages"])


def test_codex_app_server_turn_gets_full_message_and_returns_clean_history(monkeypatch):
    """The codex app-server runtime takes user_message directly and returns
    without _persist_session."""
    from agent.transports.codex_app_server_session import (
        CodexAppServerSession,
        TurnResult,
    )

    seen = []

    def fake_run_turn(self, user_input, **kwargs):
        seen.append(user_input)
        return TurnResult(
            final_text="hi", projected_messages=[{"role": "assistant", "content": "hi"}],
            tool_iterations=0, interrupted=False, error=None,
            turn_id="turn-1", thread_id="thread-1",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(CodexAppServerSession, "ensure_started", lambda self: "thread-1")
    agent = AIAgent(
        api_key="stub", base_url="https://stub.invalid", provider="openai",
        api_mode="codex_app_server", quiet_mode=True,
        skip_context_files=True, skip_memory=True,
    )

    with patch.object(agent, "_spawn_background_review", return_value=None):
        result = agent.run_conversation(
            VOICE_PREFIX + "Hello there", persist_user_message="Hello there",
        )

    assert seen == [VOICE_PREFIX + "Hello there"]
    assert _user_contents(result["messages"]) == ["Hello there"]
    _assert_no_private_keys(result["messages"])
