"""Cache-prefix byte-stability regression tests.

The #1 cost invariant of the conversation loop: across consecutive API calls,
the serialized message prefix must be BYTE-IDENTICAL — provider prompt caches
key on the exact prefix, so any drift (recomputed per-turn injections, mutated
history, a rebuilt system prompt) silently re-bills the whole context on every
iteration. Previously enforced only by comments and code review
(docs/agi-roadmap-2026.md, Track D item 1); these tests make it executable.

Strategy: drive the real ``run_conversation`` loop with a fake client that
records every outbound ``messages`` payload, force multiple API iterations in
one turn via tool calls, and assert each call's payload extends — never
rewrites — the previous call's serialized bytes. The per-turn injection
sources (lesson recall, memory prefetch) are mocked to return DIFFERENT text
on every invocation, so any regression from compute-once-per-turn to
compute-per-API-call changes the bytes and fails the prefix assertion.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

# Each test drives several full run_conversation iterations (system-prompt
# build, cache-control, sanitization passes) per API call, so a single test
# can approach the global 30s per-test cap when the suite runs 20+ files in
# parallel. Raise the cap for this file so the hard CI gate stays reliable
# under load rather than flaking on a signal-timeout.
pytestmark = pytest.mark.timeout(120)


def _tool_call(call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="probe_tool", arguments="{}"),
    )


def _response(content=None, tool_calls=None, finish_reason="stop"):
    msg = SimpleNamespace(content=content, reasoning=None, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent() -> AIAgent:
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
            max_iterations=10,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "SYSTEM-PROMPT-BYTES must stay byte-stable."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = []
    return agent


def _run_turn(agent, prompt: str, n_tool_iterations: int = 2,
              conversation_history=None):
    """One turn with ``n_tool_iterations`` tool-call rounds then a final answer.
    Returns the list of ``messages`` payloads the client saw (one per API call).
    """
    agent.client.chat.completions.create.side_effect = (
        [_response(tool_calls=[_tool_call(f"call_{i}")],
                   finish_reason="tool_calls") for i in range(n_tool_iterations)]
        + [_response(content="final answer")]
    )
    with (
        patch("run_agent.handle_function_call",
              return_value=json.dumps({"ok": True})),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            prompt, conversation_history=conversation_history)
    payloads = [
        json.dumps(c.kwargs["messages"], sort_keys=True, ensure_ascii=False)
        for c in agent.client.chat.completions.create.call_args_list
    ]
    agent.client.chat.completions.create.reset_mock(side_effect=True)
    return result, payloads


def _assert_prefix_stable(payloads):
    """Each API call's serialized messages must start with the previous call's
    serialized messages minus the closing bracket (i.e. the prior list is a
    byte-identical prefix of the next)."""
    assert len(payloads) >= 2, "need multiple API calls to test prefix stability"
    for prev, cur in zip(payloads, payloads[1:]):
        prev_body = prev[:-1]  # drop the closing ']'
        assert cur[:len(prev_body)] == prev_body, (
            "cache prefix drifted between consecutive API calls:\n"
            f"prev: {prev_body[:400]}\n cur: {cur[:400]}"
        )


def test_prefix_bytes_stable_across_iterations_within_a_turn():
    agent = _make_agent()
    _, payloads = _run_turn(agent, "do a thing", n_tool_iterations=2)
    assert len(payloads) == 3
    _assert_prefix_stable(payloads)


def test_prefix_stable_even_when_injection_sources_vary_per_call():
    """The per-turn injections (lesson recall, memory prefetch) must be
    computed ONCE per turn. Mock them to return different bytes on every
    invocation — if the loop ever recomputes them per API iteration, the
    current turn's user message changes between calls and the prefix breaks.
    """
    agent = _make_agent()
    counter = {"n": 0}

    def varying_recall(query, **kwargs):
        counter["n"] += 1
        return f"<lessons-context call={counter['n']}>use tabs not spaces</lessons-context>"

    with patch("agent.lessons.recall_context_for_turn", side_effect=varying_recall):
        _, payloads = _run_turn(agent, "do a thing", n_tool_iterations=2)

    assert len(payloads) == 3
    _assert_prefix_stable(payloads)
    # The injection must actually be present in the outbound user message —
    # otherwise this test would vacuously pass on a broken injection path.
    assert "<lessons-context call=1>" in payloads[0]


def test_prior_turn_bytes_untouched_by_next_turn():
    """Turn 2 must extend turn 1's history verbatim: no inherited injections
    re-rendered differently, no mutation of persisted turn-1 messages."""
    agent = _make_agent()
    result1, payloads1 = _run_turn(agent, "first task", n_tool_iterations=1)

    result2, payloads2 = _run_turn(
        agent, "second task", n_tool_iterations=1,
        conversation_history=result1["messages"])

    # Every message dict from turn 1's final API payload must appear
    # byte-identically inside turn 2's first payload.
    turn1_msgs = json.loads(payloads1[-1])
    turn2_msgs = json.loads(payloads2[0])
    turn2_serialized = [json.dumps(m, sort_keys=True, ensure_ascii=False)
                        for m in turn2_msgs]
    for msg in turn1_msgs:
        s = json.dumps(msg, sort_keys=True, ensure_ascii=False)
        assert s in turn2_serialized, (
            f"turn-1 message rewritten or dropped in turn 2: {s[:200]}"
        )
    _assert_prefix_stable(payloads2)


def test_lesson_injection_survives_replay_across_turns():
    """THE regression this file existed to catch and didn't: lesson recall used
    to ride only the per-call API copy, so turn 1's user message was sent WITH
    the lessons block but replayed WITHOUT it on turn 2 — tearing the cache
    prefix at that message on every subsequent turn. The block must now be part
    of the canonical persisted message and replay byte-identically."""
    agent = _make_agent()
    counter = {"n": 0}

    def varying_recall(query, **kwargs):
        counter["n"] += 1
        return f"<lessons-context call={counter['n']}>prefer rsync</lessons-context>"

    with patch("agent.lessons.recall_context_for_turn", side_effect=varying_recall):
        result1, payloads1 = _run_turn(agent, "first task", n_tool_iterations=1)
        result2, payloads2 = _run_turn(
            agent, "second task", n_tool_iterations=1,
            conversation_history=result1["messages"])

    # turn 1's outbound user message carried the block...
    assert "<lessons-context call=1>" in payloads1[0]
    # ...and turn 2 replays that exact message — block included — while the
    # new turn gets its own (different) recall.
    turn1_msgs = json.loads(payloads1[-1])
    turn2_serialized = [json.dumps(m, sort_keys=True, ensure_ascii=False)
                        for m in json.loads(payloads2[0])]
    for msg in turn1_msgs:
        s = json.dumps(msg, sort_keys=True, ensure_ascii=False)
        assert s in turn2_serialized, (
            f"turn-1 message (incl. lessons block) rewritten in turn 2: {s[:200]}")
    _assert_prefix_stable(payloads2)
