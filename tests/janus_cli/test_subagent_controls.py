"""``/agents`` subagent listing and stop/steer controls (CLI + TUI backend)."""

from __future__ import annotations

import pytest

from janus_cli import subagent_controls
from tools import delegate_tool


class _FakeChild:
    def __init__(self):
        self.interrupted = []
        self.steered = []

    def interrupt(self, reason):
        self.interrupted.append(reason)

    def steer(self, text):
        self.steered.append(text)
        return True


@pytest.fixture
def child():
    fake = _FakeChild()
    delegate_tool._register_subagent(
        {
            "subagent_id": "sa-1",
            "parent_id": None,
            "depth": 0,
            "goal": "Audit the gateway retry helpers and report duplicates",
            "model": "m",
            "started_at": 1000.0,
            "status": "running",
            "tool_count": 4,
            "agent": fake,
        }
    )
    delegate_tool._register_subagent(
        {
            "subagent_id": "sa-2",
            "parent_id": "sa-1",
            "depth": 1,
            "goal": "Read feishu.py",
            "model": "m",
            "started_at": 1060.0,
            "status": "running",
            "tool_count": 1,
            "agent": _FakeChild(),
        }
    )
    yield fake
    delegate_tool._unregister_subagent("sa-1")
    delegate_tool._unregister_subagent("sa-2")


def test_rows_show_id_elapsed_tools_and_goal_nested_by_depth(child):
    rows = subagent_controls.subagent_rows(delegate_tool.list_active_subagents(), now=1125.0)

    assert len(rows) == 2
    parent, nested = rows
    assert "sa-1" in parent and "2m 5s" in parent and "4 tools" in parent and "Audit the gateway" in parent
    assert nested.startswith("  ") and "sa-2" in nested and "1 tool ·" in nested
    assert len(nested) - len(nested.lstrip()) > len(parent) - len(parent.lstrip())


def test_long_goals_are_shortened():
    rows = subagent_controls.subagent_rows(
        [{"subagent_id": "x", "depth": 0, "goal": "word " * 40, "started_at": 0, "tool_count": 0}],
        now=1.0,
    )
    assert rows[0].endswith("…")
    assert len(rows[0]) < 140


def test_stop_interrupts_the_child(child):
    message = subagent_controls.run_agents_action("stop sa-1")
    assert child.interrupted and "sa-1" in message


def test_steer_sends_the_text(child):
    message = subagent_controls.run_agents_action("steer sa-1 focus on feishu only")
    assert child.steered == ["focus on feishu only"]
    assert "sa-1" in message


def test_unknown_child_is_reported(child):
    assert "no running subagent" in subagent_controls.run_agents_action("stop nope").lower()


@pytest.mark.parametrize("arg", ["stop", "steer sa-1", "launch sa-1"])
def test_bad_usage_returns_help(arg, child):
    message = subagent_controls.run_agents_action(arg)
    assert "/agents stop <id>" in message and "/agents steer <id> <message>" in message


def test_no_arguments_means_list():
    assert subagent_controls.run_agents_action("") is None
    assert subagent_controls.run_agents_action("   ") is None


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def _cli(monkeypatch):
    import cli as cli_mod

    printed = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda text="", *a, **k: printed.append(str(text)))
    obj = object.__new__(cli_mod.JanusCLI)
    obj._agent_running = True
    return obj, printed


def test_cli_agents_lists_running_subagents(monkeypatch, child):
    obj, printed = _cli(monkeypatch)
    obj._handle_agents_command("/agents")
    out = "\n".join(printed)
    assert "Subagents: 2" in out
    assert "sa-1" in out and "sa-2" in out


def test_cli_agents_steer_reaches_the_child(monkeypatch, child):
    obj, printed = _cli(monkeypatch)
    obj._handle_agents_command("/agents steer sa-1 stop after the first file")
    assert child.steered == ["stop after the first file"]
    assert not any("Running processes" in line for line in printed)


# ---------------------------------------------------------------------------
# TUI gateway RPC
# ---------------------------------------------------------------------------

def test_tui_rpc_steers_a_subagent(child):
    from tui_gateway import server

    reply = server.handle_request(
        {"id": "1", "method": "subagent.steer", "params": {"subagent_id": "sa-1", "text": "hurry"}}
    )
    assert reply["result"] == {"steered": True, "subagent_id": "sa-1"}
    assert child.steered == ["hurry"]


def test_tui_rpc_reports_a_missing_subagent(child):
    from tui_gateway import server

    reply = server.handle_request(
        {"id": "1", "method": "subagent.steer", "params": {"subagent_id": "gone", "text": "x"}}
    )
    assert reply["result"]["steered"] is False
    assert "gone" in reply["result"]["error"]
