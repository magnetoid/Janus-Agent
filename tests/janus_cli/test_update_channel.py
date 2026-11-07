"""Release channels for ``janus update`` (``--channel stable|main``)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from janus_cli import update_channel


GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
       "-c", "tag.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        GIT + list(args), cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "a")
    return root


class TestLatestReleaseTag:
    def test_picks_highest_calver_and_ignores_other_tags(self, repo):
        for tag in ("v2026.11.9", "v2026.11.10", "v2026.11.10.2", "v2026.2.1",
                    "morpheus-known-good", "v2026.12.1-rc1"):
            _git(repo, "tag", tag)
        assert update_channel.latest_release_tag(["git"], repo) == "v2026.11.10.2"

    def test_none_when_nothing_was_released(self, repo):
        _git(repo, "tag", "morpheus-known-good")
        assert update_channel.latest_release_tag(["git"], repo) is None


class TestClassifyMove:
    def test_current_when_head_is_the_release(self, repo):
        _git(repo, "tag", "v2026.11.1")
        assert update_channel.classify_move(["git"], repo, "v2026.11.1") == "current"

    def test_forward_when_release_is_ahead(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "b")
        _git(repo, "tag", "v2026.11.1")
        _git(repo, "checkout", "-q", "--detach", base)
        assert update_channel.classify_move(["git"], repo, "v2026.11.1") == "forward"
        assert update_channel.commits_between(["git"], repo, "HEAD", "v2026.11.1") == 1

    def test_rollback_when_checkout_is_newer(self, repo):
        _git(repo, "tag", "v2026.11.1")
        _commit(repo, "b")
        assert update_channel.classify_move(["git"], repo, "v2026.11.1") == "rollback"

    def test_shallow_install_is_deepened_before_calling_it_a_rollback(self, repo, tmp_path):
        # scripts/install.sh clones with --depth 1; after moving to an older
        # release the ancestry walk to a newer one stops at the shallow edge.
        _git(repo, "tag", "v2026.11.1")
        _commit(repo, "b")
        _commit(repo, "c")
        _git(repo, "tag", "v2026.11.2")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(clone)], check=True)
        _git(clone, "fetch", "-q", "origin", "--tags")
        _git(clone, "checkout", "-q", "--detach", "v2026.11.1")

        assert update_channel.classify_move(["git"], clone, "v2026.11.2") == "forward"


@pytest.mark.parametrize(
    "origin",
    ["git@github.com:magnetoid/Janus-Agent.git", "https://github.com/magnetoid/Janus-Agent"],
)
def test_release_notes_url_for_github_remotes(origin):
    assert (
        update_channel.release_notes_url(origin, "v2026.11.1")
        == "https://github.com/magnetoid/Janus-Agent/releases/tag/v2026.11.1"
    )


def test_release_notes_url_is_none_off_github():
    assert update_channel.release_notes_url("https://gitlab.com/a/b.git", "v1") is None
    assert update_channel.release_notes_url(None, "v1") is None


class TestResolveChannel:
    def test_defaults_to_main(self):
        assert update_channel.resolve_channel(None, None, {}) == "main"

    def test_configured_channel_is_used(self):
        assert update_channel.resolve_channel(None, None, {"updates": {"channel": "stable"}}) == "stable"

    def test_flag_overrides_config(self):
        cfg = {"updates": {"channel": "stable"}}
        assert update_channel.resolve_channel("main", None, cfg) == "main"

    def test_branch_flag_means_tracking_a_branch(self):
        cfg = {"updates": {"channel": "stable"}}
        assert update_channel.resolve_channel(None, "feat/x", cfg) == "main"

    def test_stable_with_branch_is_refused(self):
        with pytest.raises(ValueError, match="--branch"):
            update_channel.resolve_channel("stable", "feat/x", {})

    def test_unknown_channel_is_refused(self):
        with pytest.raises(ValueError, match="stable"):
            update_channel.resolve_channel("nightly", None, {})

    def test_unknown_configured_channel_falls_back_to_main(self):
        assert update_channel.resolve_channel(None, None, {"updates": {"channel": "beta"}}) == "main"


def test_save_channel_refuses_to_overwrite_an_unparseable_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    broken = "model:\n  default: [unclosed\n"
    cfg_path.write_text(broken, encoding="utf-8")
    monkeypatch.setattr("janus_cli.config.get_config_path", lambda: cfg_path)

    with pytest.raises(ValueError, match="config.yaml"):
        update_channel.save_channel("stable")
    assert cfg_path.read_text(encoding="utf-8") == broken


def test_save_channel_writes_only_the_user_key(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": {"default": "x"}}), encoding="utf-8")
    monkeypatch.setattr("janus_cli.config.get_config_path", lambda: cfg_path)

    update_channel.save_channel("stable")

    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written == {"model": {"default": "x"}, "updates": {"channel": "stable"}}


# ---------------------------------------------------------------------------
# cmd_update wiring (subprocess faked, same harness style as test_cmd_update)
# ---------------------------------------------------------------------------

def _fake_git(*, head="aaa", tag_sha="bbb", head_is_ancestor=True, tags="v2026.11.1\n"):
    def run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        if "tag --list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=tags, stderr="")
        if "rev-parse" in joined and "^{commit}" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tag_sha}\n", stderr="")
        if "rev-parse" in joined and joined.endswith("HEAD"):
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{head}\n", stderr="")
        if "merge-base --is-ancestor" in joined:
            return subprocess.CompletedProcess(cmd, 0 if head_is_ancestor else 1, stdout="", stderr="")
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="4\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


def _commands(mock_run):
    return [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]


@pytest.fixture
def stable_config(monkeypatch):
    saved = []
    monkeypatch.setattr("janus_cli.update_channel.save_channel", saved.append)
    monkeypatch.setattr("janus_cli.config.load_config", lambda: {"updates": {"channel": "stable"}})
    # Stop right after the code moves: the dependency/rebuild half of the
    # update is covered elsewhere and is not channel-specific.
    monkeypatch.setattr("janus_cli.main._clear_bytecode_cache", lambda root: (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr("janus_cli.main._validate_critical_files_syntax", lambda root: (True, None, None))
    return saved


@patch("shutil.which", return_value=None)
@patch("subprocess.run")
def test_stable_update_checks_out_the_release_tag(mock_run, _which, stable_config):
    from janus_cli.main import cmd_update

    mock_run.side_effect = _fake_git()
    with pytest.raises(SystemExit):
        cmd_update(SimpleNamespace())

    cmds = _commands(mock_run)
    assert any(c.endswith("fetch origin --tags") for c in cmds)
    assert any(c.endswith("checkout --detach v2026.11.1") for c in cmds)
    assert not any(" pull " in f" {c} " for c in cmds)


@patch("shutil.which", return_value=None)
@patch("subprocess.run")
def test_stable_update_refuses_rollback_without_flag(mock_run, _which, stable_config, capsys):
    from janus_cli.main import cmd_update

    mock_run.side_effect = _fake_git(head_is_ancestor=False)
    cmd_update(SimpleNamespace())

    assert not any("checkout --detach" in c for c in _commands(mock_run))
    assert "--allow-rollback" in capsys.readouterr().out


@patch("shutil.which", return_value=None)
@patch("subprocess.run")
def test_stable_update_rolls_back_when_allowed(mock_run, _which, stable_config):
    from janus_cli.main import cmd_update

    mock_run.side_effect = _fake_git(head_is_ancestor=False)
    with pytest.raises(SystemExit):
        cmd_update(SimpleNamespace(allow_rollback=True))

    assert any(c.endswith("checkout --detach v2026.11.1") for c in _commands(mock_run))


@patch("shutil.which", return_value=None)
@patch("subprocess.run")
def test_stable_update_is_a_no_op_on_the_current_release(mock_run, _which, stable_config, capsys):
    from janus_cli.main import cmd_update

    mock_run.side_effect = _fake_git(head="same", tag_sha="same")
    cmd_update(SimpleNamespace())

    cmds = _commands(mock_run)
    assert not any("checkout" in c or " pull " in f" {c} " for c in cmds)
    assert "v2026.11.1" in capsys.readouterr().out


@patch("shutil.which", return_value=None)
@patch("subprocess.run")
def test_channel_flag_is_persisted(mock_run, _which, stable_config):
    from janus_cli.main import cmd_update

    mock_run.side_effect = _fake_git(head="same", tag_sha="same")
    cmd_update(SimpleNamespace(channel="stable"))

    assert stable_config == ["stable"]


# ---------------------------------------------------------------------------
# Startup "update available" check follows the channel
# ---------------------------------------------------------------------------

class TestBannerCheck:
    def test_stable_counts_commits_up_to_a_newer_release(self, repo):
        from janus_cli.banner import _check_release_via_local_git

        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "b")
        _commit(repo, "c")
        _git(repo, "tag", "v2026.11.1")
        _git(repo, "checkout", "-q", "--detach", base)
        assert _check_release_via_local_git(repo) == 2

    def test_stable_ignores_main_commits_past_the_release(self, repo):
        from janus_cli.banner import _check_release_via_local_git

        _git(repo, "tag", "v2026.11.1")
        _commit(repo, "b")
        assert _check_release_via_local_git(repo) == 0

    def test_check_for_updates_uses_the_release_check_on_stable(self, tmp_path, monkeypatch):
        import json
        import time

        import janus_cli.banner as banner
        from janus_cli import __version__

        monkeypatch.setenv("JANUS_HOME", str(tmp_path))
        monkeypatch.delenv("JANUS_REVISION", raising=False)
        # A fresh cache entry written while on main must not answer for stable.
        (tmp_path / ".update_check").write_text(
            json.dumps({"ts": time.time(), "behind": 40, "ver": __version__}), encoding="utf-8"
        )
        monkeypatch.setattr("janus_cli.config.load_config", lambda: {"updates": {"channel": "stable"}})
        monkeypatch.setattr(banner, "_check_release_via_local_git", lambda repo_dir: 2)
        monkeypatch.setattr(banner, "_check_via_local_git", lambda repo_dir: pytest.fail("main check used"))
        fake_banner = tmp_path / "janus_cli" / "banner.py"
        fake_banner.parent.mkdir()
        fake_banner.touch()
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(banner, "__file__", str(fake_banner))

        assert banner.check_for_updates() == 2


@patch("subprocess.run")
def test_check_on_stable_reports_the_newer_release(mock_run, stable_config, capsys):
    from janus_cli.main import cmd_update

    def run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "remote get-url" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="git@github.com:o/r.git\n", stderr="")
        return _fake_git()(cmd, **kwargs)

    mock_run.side_effect = run
    cmd_update(SimpleNamespace(check=True))

    out = capsys.readouterr().out
    assert "v2026.11.1" in out
    assert "https://github.com/o/r/releases/tag/v2026.11.1" in out
    assert not any("checkout" in c for c in _commands(mock_run))
