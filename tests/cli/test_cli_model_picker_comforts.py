"""CLI wiring for the P5 comforts: git branch in the status bar, wait notifications."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from cli import JanusCLI


def _make_cli(**attrs):
    cli_obj = JanusCLI.__new__(JanusCLI)
    cli_obj.model = "anthropic/claude-sonnet-4-20250514"
    cli_obj.session_start = datetime.now() - timedelta(minutes=3)
    cli_obj.conversation_history = []
    cli_obj.agent = None
    for name, value in attrs.items():
        setattr(cli_obj, name, value)
    return cli_obj


def _repo(tmp_path, branch="feat/status"):
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return git_dir.parent


class TestStatusBarGitBranch:
    def test_branch_shown_on_wide_terminal_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", str(_repo(tmp_path)))
        cli_obj = _make_cli(status_bar_git_branch=True)

        assert "feat/status" in cli_obj._build_status_bar_text(width=120)
        cli_obj._status_bar_visible = True
        cli_obj._get_tui_terminal_width = lambda: 120
        fragments = "".join(text for _style, text in cli_obj._get_status_bar_fragments())
        assert "feat/status" in fragments

    def test_branch_hidden_when_disabled_or_narrow(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", str(_repo(tmp_path)))

        assert "feat/status" not in _make_cli()._build_status_bar_text(width=120)
        enabled = _make_cli(status_bar_git_branch=True)
        assert "feat/status" not in enabled._build_status_bar_text(width=60)

    def test_outside_a_repo_shows_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        cli_obj = _make_cli(status_bar_git_branch=True)

        assert "⎇" not in cli_obj._build_status_bar_text(width=120)


class TestNotifyOnWait:
    def _answer_approval(self, cli_obj, monkeypatch, calls):
        import janus_cli.wait_notify as wait_notify

        monkeypatch.setattr(wait_notify, "notify_waiting", lambda title, msg, **kw: calls.append((title, msg)))
        cli_obj._approval_lock = threading.Lock()
        cli_obj._approval_state = None
        cli_obj._approval_deadline = 0
        cli_obj._invalidate = lambda *a, **k: None

        def responder():
            for _ in range(200):
                state = cli_obj._approval_state
                if state:
                    state["response_queue"].put("deny")
                    return
                time.sleep(0.01)

        threading.Thread(target=responder, daemon=True).start()
        return cli_obj._approval_callback("rm -rf build", "recursive delete")

    def test_approval_notifies_when_enabled(self, monkeypatch):
        calls = []
        result = self._answer_approval(_make_cli(notify_on_wait=True), monkeypatch, calls)

        assert result == "deny"
        assert len(calls) == 1
        assert "rm -rf build" in calls[0][1]

    def test_approval_is_quiet_by_default(self, monkeypatch):
        calls = []
        self._answer_approval(_make_cli(), monkeypatch, calls)

        assert calls == []


class TestModelReasoningFlag:
    def _cli(self, monkeypatch, saved):
        import cli as cli_mod

        monkeypatch.setattr(cli_mod, "_cprint", lambda *a, **k: None)
        monkeypatch.setattr(cli_mod, "save_config_value", lambda k, v: saved.append((k, v)) or True)
        agent = type("Agent", (), {"reasoning_config": None, "switch_model": lambda self, **kw: None})()
        return _make_cli(
            agent=agent, provider="anthropic", requested_provider="anthropic",
            base_url="", api_key="", api_mode="", reasoning_config=None,
            _explicit_api_key="", _explicit_base_url="",
        )

    def test_reasoning_alone_updates_session_and_live_agent(self, monkeypatch):
        saved = []
        cli_obj = self._cli(monkeypatch, saved)
        monkeypatch.setattr(
            "janus_cli.model_switch.switch_model",
            lambda **kw: (_ for _ in ()).throw(AssertionError("no switch expected")),
        )

        cli_obj._handle_model_switch("/model --reasoning xhigh")

        expected = {"enabled": True, "effort": "xhigh"}
        assert cli_obj.reasoning_config == expected
        assert cli_obj.agent.reasoning_config == expected
        assert saved == []

    def test_switch_with_reasoning_global_persists_both(self, monkeypatch):
        from janus_cli.model_switch import ModelSwitchResult

        saved = []
        cli_obj = self._cli(monkeypatch, saved)
        monkeypatch.setattr(
            "janus_cli.model_switch.switch_model",
            lambda **kw: ModelSwitchResult(
                success=True, new_model="claude-opus", target_provider="anthropic",
                provider_changed=False, api_key="", base_url="", api_mode="",
            ),
        )
        monkeypatch.setattr("janus_cli.model_switch.resolve_display_context_length", lambda *a, **k: None)

        cli_obj._handle_model_switch("/model claude-opus --reasoning none --global")

        assert cli_obj.model == "claude-opus"
        assert cli_obj.agent.reasoning_config == {"enabled": False}
        assert ("model.default", "claude-opus") in saved
        assert ("agent.reasoning_effort", "none") in saved

    def test_unknown_level_changes_nothing(self, monkeypatch):
        saved = []
        cli_obj = self._cli(monkeypatch, saved)

        cli_obj._handle_model_switch("/model claude-opus --reasoning loud")

        assert cli_obj.model == "anthropic/claude-sonnet-4-20250514"
        assert cli_obj.reasoning_config is None
