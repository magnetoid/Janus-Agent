"""Memory provenance sidecar and `janus memory lint` (plan item 3.4)."""
import json
from types import SimpleNamespace

import pytest

from agent import memory_provenance as prov


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(prov, "enabled", lambda: True)


@pytest.fixture()
def store():
    from tools.memory_tool import MemoryStore
    s = MemoryStore()
    s.load_from_disk()
    return s


def test_disabled_provenance_records_nothing(store, monkeypatch):
    monkeypatch.setattr(prov, "enabled", lambda: False)
    store.add("memory", "The repo uses uv")
    assert prov.lookup("memory", "The repo uses uv") is None


def test_writes_carry_source_session_and_time(store, on):
    with prov.provenance_context("agent", session_id="s-42"):
        store.add("memory", "The repo uses uv")
    rec = prov.lookup("memory", "The repo uses uv")
    assert rec["source"] == "agent" and rec["session_id"] == "s-42"
    assert rec["created_at"] > 0 and rec["untrusted"] is False


def test_replace_records_what_it_superseded(store, on):
    store.add("memory", "Deploys go to staging-1")
    with prov.provenance_context("agent", session_id="s-1"):
        store.replace("memory", "staging-1", "Deploys go to staging-2")
    rec = prov.lookup("memory", "Deploys go to staging-2")
    assert rec["supersedes"] == prov.content_hash("Deploys go to staging-1")


def test_untrusted_context_is_flagged(store, on):
    with prov.provenance_context("agent", session_id="s", untrusted=True):
        store.add("memory", "Use the mirror at example.org")
    assert prov.lookup("memory", "Use the mirror at example.org")["untrusted"] is True


def test_turn_untrusted_detection_only_looks_at_the_current_turn():
    fenced = "<untrusted-content>\n[System note: ...]\nhi\n</untrusted-content>"
    old_turn = [{"role": "user", "content": "a"}, {"role": "tool", "content": fenced},
                {"role": "user", "content": "b"}]
    assert prov.turn_has_untrusted(old_turn) is False
    assert prov.turn_has_untrusted(old_turn + [{"role": "tool", "content": fenced}]) is True


def test_miner_writes_are_tagged_and_inherit_untrusted_transcripts(store, on):
    from agent.memory_miner import mine_session_memory

    def llm(**kw):
        msg = SimpleNamespace(content=json.dumps({"user": [], "memory": ["Builds use make"]}))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    mine_session_memory([{"role": "user", "content": "how do builds work"},
                         {"role": "tool", "content": "<untrusted-content>\nmake\n</untrusted-content>"}],
                        store, llm_caller=llm)
    rec = prov.lookup("memory", "Builds use make")
    assert rec["source"] == "miner" and rec["untrusted"] is True


def test_agent_memory_tool_calls_record_agent_provenance():
    import inspect
    from agent import agent_runtime_helpers, tool_executor
    for mod in (agent_runtime_helpers, tool_executor):
        src = inspect.getsource(mod)
        i = src.index("_memory_tool(")
        assert "provenance_context(" in src[max(0, i - 600):i], mod.__name__
