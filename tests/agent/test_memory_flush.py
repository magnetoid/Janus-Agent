"""Memory flush before compaction (plan item 2.1).

Before the compressor discards context, durable facts from the not-yet-flushed
part of the conversation are mined into MEMORY.md / USER.md. Off by default;
never blocks compaction; the same messages are never mined twice.
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import memory_flush


def _reply(payload):
    msg = SimpleNamespace(content=json.dumps(payload))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeLLM:
    def __init__(self, payload=None, error=None):
        self.calls = []
        self.payload = payload if payload is not None else {"user": [], "memory": []}
        self.error = error

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return _reply(self.payload)

    def transcript(self, i=-1):
        return self.calls[i]["messages"][-1]["content"]


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(memory_flush, "flush_enabled", lambda: True)
    monkeypatch.setattr(memory_flush, "_frozen", lambda: False)


def _agent():
    agent = SimpleNamespace(_memory_store=object(), session_id="s1")
    return agent


def _msgs(*texts):
    out = [{"role": "system", "content": "system prompt"}]
    for i, t in enumerate(texts):
        out.append({"role": "user" if i % 2 == 0 else "assistant", "content": t})
    return out


def _memory_file():
    from tools.memory_tool import get_memory_dir
    return get_memory_dir() / "MEMORY.md"


def test_disabled_flush_spends_nothing(monkeypatch):
    monkeypatch.setattr(memory_flush, "flush_enabled", lambda: False)
    llm = FakeLLM()
    assert memory_flush.flush_before_compaction(_agent(), _msgs("hi"), llm_caller=llm) is None
    assert llm.calls == []


def test_facts_from_the_conversation_reach_memory_on_disk(enabled):
    llm = FakeLLM({"user": [], "memory": ["The deploy script lives in ops/deploy.sh"]})
    result = memory_flush.flush_before_compaction(
        _agent(), _msgs("where is deploy?", "ops/deploy.sh"), llm_caller=llm)
    assert result and result["added"]["memory"] == 1
    assert len(llm.calls) == 1
    assert "ops/deploy.sh" in llm.transcript()
    assert "The deploy script lives in ops/deploy.sh" in _memory_file().read_text(encoding="utf-8")


def test_already_flushed_messages_are_not_mined_again(enabled):
    agent = _agent()
    llm = FakeLLM({"user": [], "memory": ["fact one"]})
    first = _msgs("alpha question", "alpha answer")
    memory_flush.flush_before_compaction(agent, first, llm_caller=llm)

    # nothing new since the last flush: no second model call
    assert memory_flush.flush_before_compaction(agent, list(first), llm_caller=llm)["skipped"]
    assert len(llm.calls) == 1

    # only the new turn is sent next time
    memory_flush.flush_before_compaction(agent, first + [{"role": "user", "content": "beta"}],
                                         llm_caller=llm)
    assert len(llm.calls) == 2
    assert "beta" in llm.transcript() and "alpha question" not in llm.transcript()
    assert _memory_file().read_text(encoding="utf-8").count("fact one") == 1


def test_a_failing_model_is_reported_not_raised(enabled):
    llm = FakeLLM(error=RuntimeError("provider down"))
    result = memory_flush.flush_before_compaction(_agent(), _msgs("x", "y"), llm_caller=llm)
    assert result is not None and result.get("error")


def test_no_flush_without_memory_or_while_frozen(enabled, monkeypatch):
    llm = FakeLLM()
    no_memory = SimpleNamespace(_memory_store=None, session_id="s")
    assert memory_flush.flush_before_compaction(no_memory, _msgs("x"), llm_caller=llm) is None
    monkeypatch.setattr(memory_flush, "_frozen", lambda: True)
    assert memory_flush.flush_before_compaction(_agent(), _msgs("x"), llm_caller=llm) is None
    assert llm.calls == []


def test_flush_enabled_reads_the_memory_config(monkeypatch):
    monkeypatch.delenv("JANUS_FLAG_MEMORY__FLUSH_BEFORE_COMPACTION", raising=False)
    with patch("agent.feature_flags._config_value", return_value=None):
        assert memory_flush.flush_enabled() is False
    with patch("agent.feature_flags._config_value", return_value=True):
        assert memory_flush.flush_enabled() is True


def _compress_agent(tmp_path):
    from janus_state import SessionDB
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        model="test/model", quiet_mode=True, skip_context_files=True,
                        skip_memory=True, session_db=SessionDB(db_path=tmp_path / "s.db"),
                        session_id="flush-session")
    compressor = MagicMock()
    compressor.compress.return_value = [{"role": "user", "content": "[CONTEXT COMPACTION] s"}]
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    return agent, compressor


def test_compaction_runs_the_flush_first_and_survives_its_failure(tmp_path):
    agent, compressor = _compress_agent(tmp_path)
    order = []
    compressor.compress.side_effect = lambda *a, **k: order.append("compress") or \
        [{"role": "user", "content": "[CONTEXT COMPACTION] s"}]

    def _boom(agent_arg, messages, **_):
        order.append("flush")
        raise RuntimeError("flush exploded")

    with patch("agent.memory_flush.flush_before_compaction", side_effect=_boom):
        agent._compress_context([{"role": "user", "content": f"m{i}"} for i in range(6)],
                                "sys", approx_tokens=10_000)
    assert order == ["flush", "compress"]
