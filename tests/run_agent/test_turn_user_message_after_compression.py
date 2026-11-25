"""Turn-scoped context still reaches the turn's user message after compression.

``run_conversation`` remembers where the turn's user message sits so it can
attach the memory-provider prefetch and ``pre_llm_call`` plugin context to it
on every API call, and append recalled lessons to it at turn start.
Compression rebuilds ``messages`` (shorter, every dict copied, sometimes with
the summary merged into that very message), so the position taken at turn
start points past the end or at some other row afterwards. These tests drive
the real loop and real compressor and look at what the client was sent.
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from janus_state import SessionDB
from run_agent import AIAgent

MEMORY = "MEMORY-MARKER-7f3"
LESSON = "LESSON-MARKER-2c9"
TURN_TEXT = "Hello there"


class _MarkerMemory(MemoryProvider):
    """An external memory provider whose prefetch is a recognisable marker."""

    @property
    def name(self) -> str:
        return "marker"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return MEMORY

    def get_tool_schemas(self):
        return []


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

    def _refuse_lookup(*_a, **_k):
        raise socket.gaierror("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    # Name lookups too: an unreachable resolver would otherwise stall the
    # auxiliary-client probes compression makes.
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_lookup)
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata",
                        lambda *a, **k: {})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length",
                        lambda *a, **k: 200_000)
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata",
                        lambda *a, **k: {})
    monkeypatch.setattr("agent.context_compressor.get_model_context_length",
                        lambda *a, **k: 200_000)
    # Compression resolves an auxiliary client. Where `gh` is signed in, the
    # credential pool would read `gh auth token` and send it to the Copilot
    # token exchange.
    monkeypatch.setattr("janus_cli.copilot_auth._try_gh_cli_token",
                        lambda *a, **k: None)


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
            session_id="turn-idx-session",
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent._fallback_chain = []
    memory = MemoryManager()
    memory.add_provider(_MarkerMemory())
    agent._memory_manager = memory
    agent.compression_enabled = True
    compressor = agent.context_compressor
    compressor.protect_first_n = 2
    compressor.protect_last_n = 1
    compressor.tail_token_budget = 1
    return agent


def _history():
    history = []
    for i in range(6):
        history += [{"role": "user", "content": f"old question {i}"},
                    {"role": "assistant", "content": f"old answer {i}"}]
    return history


def _summary():
    summary = MagicMock()
    summary.choices = [MagicMock()]
    summary.choices[0].message.content = "EARLIER-WORK-SUMMARY"
    return summary


def _run_turn(agent, *, preflight: bool, n_tool_iterations: int):
    """One turn that compresses once: before the first call or after the
    first tool round. Returns the ``messages`` of every request sent."""
    agent.client.chat.completions.create.side_effect = (
        [_response(tool_calls=[_tool_call(f"call_{i}")], finish_reason="tool_calls")
         for i in range(n_tool_iterations)]
        + [_response(content="final answer")]
    )
    compressor = agent.context_compressor
    compress_now = iter([True])
    with (
        patch.object(compressor, "should_defer_preflight_to_real_usage",
                     return_value=not preflight),
        patch.object(compressor, "should_compress",
                     side_effect=lambda *_a, **_k: next(compress_now, False)),
        patch("agent.context_compressor.call_llm", return_value=_summary()),
        patch("agent.lessons.recall_context_for_turn", return_value=LESSON),
        patch("run_agent.handle_function_call", return_value=json.dumps({"ok": True})),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        agent.run_conversation(TURN_TEXT, conversation_history=_history())
    assert agent.session_id != "turn-idx-session", "compression did not run"
    return [
        c.kwargs["messages"]
        for c in agent.client.chat.completions.create.call_args_list
    ]


def _text(content) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def _rows_with(request, marker):
    return [m for m in request if marker in _text(m.get("content"))]


def test_preflight_compression_keeps_memory_and_lessons_on_the_turn_message(db):
    agent = _make_agent(db)

    requests = _run_turn(agent, preflight=True, n_tool_iterations=0)

    assert len(requests) == 1
    turn_rows = _rows_with(requests[0], TURN_TEXT)
    assert len(turn_rows) == 1 and turn_rows[0]["role"] == "user"
    assert _rows_with(requests[0], "EARLIER-WORK-SUMMARY"), "not compressed"
    for marker in (MEMORY, LESSON):
        # Exactly the turn's message carries it: nothing else is rewritten.
        assert _rows_with(requests[0], marker) == turn_rows, marker
    assert "<memory-context>" in _text(turn_rows[0]["content"])


def test_mid_turn_compression_keeps_memory_on_the_turn_message(db):
    agent = _make_agent(db)

    requests = _run_turn(agent, preflight=False, n_tool_iterations=1)

    assert len(requests) == 2
    for req in requests:
        turn_rows = _rows_with(req, TURN_TEXT)
        assert len(turn_rows) == 1 and turn_rows[0]["role"] == "user"
        assert _rows_with(req, MEMORY) == turn_rows
        assert _rows_with(req, LESSON) == turn_rows
    # The second request went out after compression.
    assert "EARLIER-WORK-SUMMARY" in "".join(
        _text(m.get("content")) for m in requests[1])


# ---------------------------------------------------------------------------
# _relocate_turn_user_message
# ---------------------------------------------------------------------------


def _relocate_turn_user_message(messages, content):
    from agent.conversation_loop import _relocate_turn_user_message as relocate
    return relocate(messages, content)


def test_relocate_finds_the_copied_message_at_its_new_index():
    msgs = [{"role": "user", "content": "old"},
            {"role": "assistant", "content": "summary"},
            {"role": "user", "content": "now"},
            {"role": "assistant", "content": "", "tool_calls": []}]
    assert _relocate_turn_user_message(msgs, "now") == 2


def test_relocate_prefers_the_latest_identical_message():
    msgs = [{"role": "user", "content": "go on"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "go on"}]
    assert _relocate_turn_user_message(msgs, "go on") == 2


def test_relocate_finds_the_message_with_the_summary_merged_in_front():
    msgs = [{"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "SUMMARY\n\n--- END ---\n\nnow"},
            {"role": "user", "content": "[System: Continue now.]"}]
    assert _relocate_turn_user_message(msgs, "now") == 2


def test_relocate_finds_multimodal_content_with_a_text_block_prepended():
    parts = [{"type": "text", "text": "what is this"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    msgs = [{"role": "assistant", "content": "a"},
            {"role": "user", "content": [{"type": "text", "text": "SUMMARY"}, *parts]}]
    assert _relocate_turn_user_message(msgs, parts) == 1


def test_relocate_returns_none_when_the_message_is_gone():
    msgs = [{"role": "user", "content": "something else"},
            {"role": "assistant", "content": "now"}]
    assert _relocate_turn_user_message(msgs, "now") is None
    assert _relocate_turn_user_message(msgs, None) is None
