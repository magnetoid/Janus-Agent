"""A delegation profile's ``budget_usd`` caps the child's own spend (plan item 3.2).

The child carries ``_session_cost_limit_usd_override``; the loop's turn-boundary
cost gate uses it (the tighter of it and ``budget.session_cost_usd``).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _response(content="done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="test/model", usage=None)


def _make_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("janus_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=5,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _response()
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _run(agent, config_limit):
    with (
        patch("agent.cost_ledger.session_cost_limit_usd", return_value=config_limit),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("go")


@pytest.mark.parametrize("config_limit", [None, 10.0])
def test_child_override_stops_the_loop_once_spent(config_limit):
    agent = _make_agent()
    agent._session_cost_limit_usd_override = 0.05
    agent.session_estimated_cost_usd = 0.06
    result = _run(agent, config_limit)
    assert result["turn_exit_reason"] == "cost_budget_exhausted"
    agent.client.chat.completions.create.assert_not_called()


def test_the_tighter_config_limit_still_wins_over_a_loose_override():
    agent = _make_agent()
    agent._session_cost_limit_usd_override = 10.0
    agent.session_estimated_cost_usd = 0.06
    result = _run(agent, 0.05)
    assert result["turn_exit_reason"] == "cost_budget_exhausted"


def test_override_under_its_cap_lets_the_turn_run():
    agent = _make_agent()
    agent._session_cost_limit_usd_override = 1.0
    agent.session_estimated_cost_usd = 0.0
    result = _run(agent, None)
    assert result["final_response"] == "done"
