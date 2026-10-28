"""Live transcripts for background children (PR #27 review).

``janus delegations attach --follow`` only reads persisted messages, so a
child flagged ``_flush_db_each_iteration`` must flush its session rows after
every tool iteration, not just at turn start and exit.
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from janus_state import SessionDB
from run_agent import AIAgent


def _tool_defs(*names):
    return [{"type": "function", "function": {"name": n, "description": n,
                                              "parameters": {"type": "object", "properties": {}}}}
            for n in names]


def _tool_call(name, call_id):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments="{}"))


def _response(content="", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)],
                           model="test/model", usage=None)


def _make_agent(session_db, session_id):
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("janus_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=10,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=session_id,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _run(agent, session_db, session_id):
    agent.client.chat.completions.create.side_effect = [
        _response(finish_reason="tool_calls", tool_calls=[_tool_call("web_search", "c1")]),
        _response(finish_reason="tool_calls", tool_calls=[_tool_call("web_search", "c2")]),
        _response(content="done"),
    ]
    seen_during_calls = []

    def _fake_tool(*_a, **_k):
        seen_during_calls.append([m["role"] for m in session_db.get_messages(session_id)])
        return json.dumps({"ok": True})

    with (
        patch("run_agent.handle_function_call", side_effect=_fake_tool),
        patch.object(agent, "_save_trajectory"),
    ):
        result = agent.run_conversation("look things up")
    return result, seen_during_calls


def test_flagged_child_flushes_each_tool_iteration_so_attach_can_follow_it(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    session_id = f"child-{uuid.uuid4().hex[:8]}"
    agent = _make_agent(session_db, session_id)
    agent._flush_db_each_iteration = True

    result, seen = _run(agent, session_db, session_id)

    assert result["final_response"] == "done"
    # by the time the second tool runs, the first iteration is already on disk
    assert seen[1] == ["user", "assistant", "tool"]
    # and the final flush adds nothing twice
    roles = [m["role"] for m in session_db.get_messages(session_id)]
    assert roles == [m["role"] for m in result["messages"] if m["role"] != "system"]
    session_db.close()


def test_unflagged_agent_keeps_the_old_persistence_points(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    session_id = f"plain-{uuid.uuid4().hex[:8]}"
    agent = _make_agent(session_db, session_id)

    result, seen = _run(agent, session_db, session_id)

    assert result["final_response"] == "done"
    assert seen[1] == ["user"]
    roles = [m["role"] for m in session_db.get_messages(session_id)]
    assert roles == [m["role"] for m in result["messages"] if m["role"] != "system"]
    session_db.close()
