"""P5 comforts: ``/model --reasoning``, git branch lookup, wait notifications."""

from __future__ import annotations

import io

import pytest

from janus_cli.model_switch import extract_reasoning_flag, parse_model_flags


class TestExtractReasoningFlag:
    def test_no_flag_leaves_args_untouched(self):
        assert extract_reasoning_flag("sonnet --global") == ("sonnet --global", None)

    def test_flag_value_is_parsed_and_removed(self):
        rest, reasoning = extract_reasoning_flag("sonnet --reasoning high --provider anthropic")
        assert reasoning == {"enabled": True, "effort": "high"}
        # The rest still parses as an ordinary /model invocation.
        assert parse_model_flags(rest) == ("sonnet", "anthropic", False, False)

    def test_none_disables_reasoning(self):
        assert extract_reasoning_flag("sonnet --reasoning none")[1] == {"enabled": False}

    def test_unicode_dash_is_accepted(self):
        # Telegram/iOS turn "--" into an em dash; parse_model_flags tolerates it too.
        rest, reasoning = extract_reasoning_flag("sonnet —reasoning low")
        assert rest == "sonnet"
        assert reasoning == {"enabled": True, "effort": "low"}

    @pytest.mark.parametrize("raw", ["sonnet --reasoning", "sonnet --reasoning loud"])
    def test_missing_or_unknown_level_raises(self, raw):
        with pytest.raises(ValueError, match="xhigh"):
            extract_reasoning_flag(raw)


class TestCurrentGitBranch:
    def _repo(self, root, head: str):
        git_dir = root / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text(head, encoding="utf-8")
        return root

    def test_branch_from_repo_root_and_subdir(self, tmp_path):
        from janus_cli.git_branch import current_git_branch

        repo = self._repo(tmp_path / "repo", "ref: refs/heads/feat/x\n")
        (repo / "a" / "b").mkdir(parents=True)
        assert current_git_branch(repo) == "feat/x"
        assert current_git_branch(repo / "a" / "b") == "feat/x"

    def test_detached_head_shows_short_sha(self, tmp_path):
        from janus_cli.git_branch import current_git_branch

        repo = self._repo(tmp_path / "repo", "0123456789abcdef0123456789abcdef01234567\n")
        assert current_git_branch(repo) == "0123456"

    def test_worktree_gitdir_file_is_followed(self, tmp_path):
        from janus_cli.git_branch import current_git_branch

        real = tmp_path / "main" / ".git" / "worktrees" / "wt"
        real.mkdir(parents=True)
        (real / "HEAD").write_text("ref: refs/heads/wt-branch\n", encoding="utf-8")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
        assert current_git_branch(wt) == "wt-branch"

    def test_outside_a_repo_returns_none(self, tmp_path):
        from janus_cli.git_branch import current_git_branch

        assert current_git_branch(tmp_path) is None

    def test_cached_lookup_reuses_result_within_ttl(self, tmp_path):
        from janus_cli.git_branch import cached_git_branch

        repo = self._repo(tmp_path / "repo", "ref: refs/heads/one\n")
        clock = [100.0]
        cache: dict = {}
        assert cached_git_branch(repo, cache=cache, ttl=10, now=lambda: clock[0]) == "one"
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/two\n", encoding="utf-8")
        assert cached_git_branch(repo, cache=cache, ttl=10, now=lambda: clock[0]) == "one"
        clock[0] += 11
        assert cached_git_branch(repo, cache=cache, ttl=10, now=lambda: clock[0]) == "two"


class TestNotifyWaiting:
    def test_rings_bell(self, monkeypatch):
        from janus_cli import wait_notify

        launched = []
        monkeypatch.setattr(wait_notify, "_launch_desktop_notification", lambda *a: launched.append(a))
        out = io.StringIO()
        wait_notify.notify_waiting("Approval needed", "rm -rf build", stream=out)
        assert out.getvalue() == "\a"
        assert launched == [("Approval needed", "rm -rf build")]

    def test_ssh_session_skips_desktop_notification(self, monkeypatch):
        from janus_cli import wait_notify

        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
        launched = []
        monkeypatch.setattr(wait_notify, "_launch_desktop_notification", lambda *a: launched.append(a))
        out = io.StringIO()
        wait_notify.notify_waiting("Approval needed", "x", stream=out)
        assert out.getvalue() == "\a"
        assert launched == []

    def test_macos_uses_osascript_with_escaped_text(self, monkeypatch):
        from janus_cli import wait_notify

        calls = []
        monkeypatch.setattr(wait_notify.sys, "platform", "darwin")
        monkeypatch.setattr(wait_notify.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(wait_notify.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
        wait_notify._launch_desktop_notification('Janus "asks"', 'say \\ "hi"')
        assert calls and calls[0][0] == "/usr/bin/osascript"
        script = calls[0][-1]
        assert '\\"asks\\"' in script
        assert 'say \\\\ \\"hi\\"' in script

    def test_linux_uses_notify_send_when_available(self, monkeypatch):
        from janus_cli import wait_notify

        calls = []
        monkeypatch.setattr(wait_notify.sys, "platform", "linux")
        monkeypatch.setattr(wait_notify.shutil, "which", lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
        monkeypatch.setattr(wait_notify.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
        wait_notify._launch_desktop_notification("Janus", "body")
        assert calls == [["/usr/bin/notify-send", "Janus", "body"]]

    def test_missing_notifier_is_silent(self, monkeypatch):
        from janus_cli import wait_notify

        monkeypatch.setattr(wait_notify.sys, "platform", "linux")
        monkeypatch.setattr(wait_notify.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            wait_notify.subprocess, "Popen",
            lambda *a, **k: pytest.fail("no notifier should be launched"),
        )
        wait_notify._launch_desktop_notification("Janus", "body")
