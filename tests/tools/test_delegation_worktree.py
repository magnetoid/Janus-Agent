"""``delegate_task(isolation="worktree")``: one git worktree per child (plan N3)."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools.delegate_tool as delegate_tool
from tools import delegation_worktree as dw


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture(autouse=True)
def _no_active_leak():
    yield
    with dw._active_lock:
        dw._active.clear()


def _branches(repo: Path) -> list[str]:
    return _git(repo, "branch", "--format=%(refname:short)").split()


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------

def test_repo_root_is_found_from_a_subdirectory(repo, tmp_path):
    sub = repo / "pkg"
    sub.mkdir()
    assert dw.repo_root_for(sub) == repo.resolve()
    outside = tmp_path / "plain"
    outside.mkdir()
    assert dw.repo_root_for(outside) is None


def test_create_checks_out_head_on_a_new_branch_under_janus_home(repo):
    wt = dw.create(repo, "parent/1", max_checkouts=8)

    assert wt.path.is_dir()
    assert dw.worktrees_root() in wt.path.parents
    assert (wt.path / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert _git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == wt.branch
    assert _git(wt.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    assert "/" not in wt.path.parent.name  # session id is sanitised into one segment


def test_finalize_keeps_work_on_the_branch_and_removes_the_checkout(repo):
    wt = dw.create(repo, "s1", max_checkouts=8)
    (wt.path / "new.txt").write_text("child work\n", encoding="utf-8")

    report = dw.finalize(wt, message="subagent sa-0: add new.txt")

    assert report["branch"] == wt.branch and report["commits"] == 1
    assert not wt.path.exists()
    assert wt.branch in _branches(repo)
    assert _git(repo, "show", f"{wt.branch}:new.txt") == "child work"
    assert not (repo / "new.txt").exists()  # the parent's checkout is untouched


def test_finalize_counts_commits_the_child_made_itself(repo):
    wt = dw.create(repo, "s1", max_checkouts=8)
    (wt.path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(wt.path, "add", "a.txt")
    _git(wt.path, "commit", "-q", "-m", "child commit")

    report = dw.finalize(wt, message="unused")

    assert report["commits"] == 1
    assert _git(repo, "log", "-1", "--format=%s", wt.branch) == "child commit"


def test_finalize_without_changes_deletes_the_branch(repo):
    wt = dw.create(repo, "s1", max_checkouts=8)

    report = dw.finalize(wt, message="noop")

    assert report["branch"] is None and report["commits"] == 0
    assert not wt.path.exists()
    assert wt.branch not in _branches(repo)


def test_cap_refuses_when_every_checkout_is_in_use(repo):
    first = dw.create(repo, "s1", max_checkouts=2)
    dw.create(repo, "s1", max_checkouts=2)

    with pytest.raises(dw.WorktreeError, match="max_checkouts"):
        dw.create(repo, "s1", max_checkouts=2)

    dw.finalize(first, message="done")
    assert dw.create(repo, "s1", max_checkouts=2).path.is_dir()


def test_cap_evicts_a_leftover_checkout_and_keeps_its_work(repo):
    leftover = dw.create(repo, "crashed", max_checkouts=1)
    (leftover.path / "partial.txt").write_text("unsaved\n", encoding="utf-8")
    with dw._active_lock:  # the process that owned it is gone
        dw._active.clear()

    fresh = dw.create(repo, "s2", max_checkouts=1)

    assert fresh.path.is_dir()
    assert not leftover.path.exists()
    assert _git(repo, "show", f"{leftover.branch}:partial.txt") == "unsaved"


def test_failing_commit_hooks_do_not_lose_the_childs_work(repo):
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    wt = dw.create(repo, "s1", max_checkouts=8)
    (wt.path / "new.txt").write_text("child work\n", encoding="utf-8")

    report = dw.finalize(wt, message="save")

    assert report["commits"] == 1 and "error" not in report
    assert _git(repo, "show", f"{wt.branch}:new.txt") == "child work"


def test_checkout_is_kept_when_its_changes_cannot_be_committed(repo, monkeypatch):
    wt = dw.create(repo, "s1", max_checkouts=8)
    (wt.path / "new.txt").write_text("child work\n", encoding="utf-8")
    monkeypatch.setattr(dw, "_commit_all", lambda path, message: False)

    report = dw.finalize(wt, message="save")

    assert report["kept_checkout"] == str(wt.path) and "error" in report
    assert (wt.path / "new.txt").read_text(encoding="utf-8") == "child work\n"
    assert wt.branch in _branches(repo)

    # A later cap check retries the save and must not drop the work either.
    with pytest.raises(dw.WorktreeError):
        dw.create(repo, "s2", max_checkouts=1)
    assert (wt.path / "new.txt").exists()


def _own_checkout_by(path: Path, pid: int) -> None:
    dw._owner_file(path).write_text(json.dumps({"pid": pid}), encoding="utf-8")
    with dw._active_lock:
        dw._active.clear()


def test_checkout_of_another_live_process_is_never_evicted(repo):
    other = subprocess.Popen(["sleep", "30"])
    try:
        theirs = dw.create(repo, "other", max_checkouts=1)
        _own_checkout_by(theirs.path, other.pid)

        with pytest.raises(dw.WorktreeError, match="max_checkouts"):
            dw.create(repo, "mine", max_checkouts=1)
        assert theirs.path.is_dir()
    finally:
        other.kill()
        other.wait()


def test_checkout_of_a_dead_process_is_evicted(repo):
    gone = subprocess.Popen(["true"])
    gone.wait()
    theirs = dw.create(repo, "other", max_checkouts=1)
    _own_checkout_by(theirs.path, gone.pid)

    assert dw.create(repo, "mine", max_checkouts=1).path.is_dir()
    assert not theirs.path.exists()


# ---------------------------------------------------------------------------
# delegate_task wiring
# ---------------------------------------------------------------------------

def _parent():
    parent = MagicMock()
    parent.session_id = "parent-1"
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


@pytest.fixture()
def wired(monkeypatch, repo):
    built = []

    def _fake_build(task_index, **kwargs):
        child = MagicMock()
        child.session_id = f"child-{task_index}"
        child._subagent_id = f"sa-{task_index}-test"
        child._session_cost_limit_usd_override = None
        built.append(kwargs)
        return child

    seen = {}

    def _fake_run_child(task_index, goal, child=None, parent_agent=None, **_):
        from agent.runtime_cwd import resolve_agent_cwd
        from tools.terminal_tool import _task_env_overrides

        cwd = Path(_task_env_overrides[child._subagent_id]["cwd"])
        seen["cwd"] = cwd
        seen["agent_cwd"] = resolve_agent_cwd()
        (cwd / f"out-{task_index}.txt").write_text("done\n", encoding="utf-8")
        return {"task_index": task_index, "status": "completed", "summary": "ok", "api_calls": 1}

    monkeypatch.setattr(delegate_tool, "_build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_tool, "_run_child", _fake_run_child)
    monkeypatch.setattr(delegate_tool, "_resolve_workspace_hint", lambda parent: str(repo))
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda *a, **k: None)
    monkeypatch.setattr("tools.terminal_tool._resolve_terminal_env", lambda: "local")
    return built, seen


def _config(monkeypatch, enabled=True, max_checkouts=8):
    monkeypatch.setattr(
        delegate_tool,
        "_load_config",
        lambda: {"worktree": {"enabled": enabled, "max_checkouts": max_checkouts}},
    )


def _delegate(**kw):
    return json.loads(delegate_tool.delegate_task(parent_agent=_parent(), **kw))


def test_child_runs_in_its_worktree_and_reports_the_branch(monkeypatch, wired, repo):
    built, seen = wired
    _config(monkeypatch)

    out = _delegate(goal="write the output file", isolation="worktree")

    result = out["results"][0]
    branch = result["worktree"]["branch"]
    assert result["worktree"]["commits"] == 1
    assert _git(repo, "show", f"{branch}:out-0.txt") == "done"
    assert not (repo / "out-0.txt").exists()
    assert seen["agent_cwd"] == seen["cwd"]
    assert built[0]["workspace_path"] == str(seen["cwd"])
    assert not seen["cwd"].exists()
    from tools.terminal_tool import _task_env_overrides
    assert "sa-0-test" not in _task_env_overrides


def test_isolation_needs_the_config_switch(monkeypatch, wired):
    _config(monkeypatch, enabled=False)
    out = _delegate(goal="x", isolation="worktree")
    assert "delegation.worktree.enabled" in out["error"]


def test_isolation_needs_a_git_repository(monkeypatch, wired, tmp_path):
    _config(monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(delegate_tool, "_resolve_workspace_hint", lambda parent: str(plain))
    out = _delegate(goal="x", isolation="worktree")
    assert "git repository" in out["error"]


def test_isolation_needs_the_local_backend(monkeypatch, wired):
    _config(monkeypatch)
    monkeypatch.setattr("tools.terminal_tool._resolve_terminal_env", lambda: "docker")
    out = _delegate(goal="x", isolation="worktree")
    assert "local" in out["error"]


def test_unknown_isolation_value_is_refused(monkeypatch, wired):
    _config(monkeypatch)
    out = _delegate(goal="x", isolation="container")
    assert "isolation" in out["error"]


def test_batch_over_the_cap_starts_nothing(monkeypatch, wired, repo):
    _config(monkeypatch, max_checkouts=1)
    monkeypatch.setattr(delegate_tool, "_get_max_concurrent_children", lambda: 3)

    out = _delegate(tasks=[{"goal": "a"}, {"goal": "b"}], isolation="worktree")

    assert "max_checkouts" in out["error"]
    assert not any(p.is_dir() for p in dw.worktrees_root().glob("*/*"))
    assert _branches(repo) == ["main"]


def test_schema_offers_isolation_only_when_enabled(monkeypatch):
    _config(monkeypatch, enabled=False)
    assert "isolation" not in delegate_tool._build_dynamic_schema_overrides()["parameters"]["properties"]
    _config(monkeypatch, enabled=True)
    props = delegate_tool._build_dynamic_schema_overrides()["parameters"]["properties"]
    assert props["isolation"]["enum"] == ["worktree"]
