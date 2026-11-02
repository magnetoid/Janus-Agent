"""Per-child git worktrees for ``delegate_task(isolation="worktree")``.

Each child gets ``git worktree add -b janus/delegate/<id>`` of the parent's
HEAD under ``<janus_home>/worktrees/<session>/<child>``. When the child is
done, its uncommitted changes are committed to that branch, the checkout is
removed, and the branch is kept only if it holds work. The parent gets the
branch name back and merges or discards it.

``max_checkouts`` caps live checkouts across all sessions. A checkout left
behind by a process that died is evicted to make room (its work is
committed to its branch first); checkouts of running children never are.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_MAX_CHECKOUTS = 8
BRANCH_PREFIX = "janus/delegate/"

_GIT_TIMEOUT = 60
# Automatic commits must not stop on the user's hooks or signing setup.
_COMMIT_FLAGS = ("-c", "commit.gpgsign=false")
_FALLBACK_IDENTITY = ("-c", "user.name=Janus", "-c", "user.email=janus@localhost")

_active: set[str] = set()
_active_lock = threading.Lock()
# Serialises the cap check with the checkout it admits (nested orchestrators
# create worktrees from several threads).
_create_lock = threading.Lock()


class WorktreeError(RuntimeError):
    """A child worktree could not be created."""


@dataclass(frozen=True)
class ChildWorktree:
    path: Path
    branch: str
    repo_root: Path
    base: str


def settings(delegation_cfg: Optional[dict]) -> tuple[bool, int]:
    """``(enabled, max_checkouts)`` from the ``delegation`` config section."""
    section = (delegation_cfg or {}).get("worktree")
    section = section if isinstance(section, dict) else {}
    try:
        cap = max(1, int(section.get("max_checkouts") or DEFAULT_MAX_CHECKOUTS))
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_CHECKOUTS
    return bool(section.get("enabled", False)), cap


def worktrees_root() -> Path:
    from janus_constants import get_janus_home

    return get_janus_home() / "worktrees"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT
    )


def repo_root_for(path: str | Path) -> Optional[Path]:
    """Top level of the git work tree containing *path*, or None."""
    try:
        result = _git(Path(path), "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip() if result.returncode == 0 else ""
    return Path(top).resolve() if top else None


def _segment(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._") or "session"


def _checkouts() -> list[Path]:
    root = worktrees_root()
    if not root.is_dir():
        return []
    return [p for session in root.iterdir() if session.is_dir() for p in session.iterdir() if p.is_dir()]


def _make_room(max_checkouts: int) -> None:
    with _active_lock:
        active = set(_active)
    existing = _checkouts()
    leftovers = sorted((p for p in existing if str(p) not in active), key=lambda p: p.stat().st_mtime)
    while len(existing) >= max_checkouts and leftovers:
        victim = leftovers.pop(0)
        _evict(victim)
        existing.remove(victim)
    if len(existing) >= max_checkouts:
        raise WorktreeError(
            f"all {max_checkouts} subagent worktrees are in use (delegation.worktree.max_checkouts); "
            "wait for a child to finish or raise the cap"
        )


def _evict(path: Path) -> None:
    """Save a leftover checkout's work on its branch, then remove it."""
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode == 0 and common.stdout.strip():
        repo = Path(common.stdout.strip())
        repo = repo.parent if repo.name == ".git" else repo
        _commit_all(path, "janus: save work from an abandoned subagent worktree")
        _git(repo, "worktree", "remove", "--force", str(path))
        _git(repo, "worktree", "prune")
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _drop_empty_parent(path)


def _drop_empty_parent(path: Path) -> None:
    try:
        path.parent.rmdir()
    except OSError:
        pass


def create(repo_root: Path, session_id: str, *, max_checkouts: int = DEFAULT_MAX_CHECKOUTS) -> ChildWorktree:
    """Check out the repo's HEAD on a fresh branch for one child."""
    repo_root = Path(repo_root).resolve()
    head = _git(repo_root, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise WorktreeError(f"{repo_root} has no commit to branch from")
    base = head.stdout.strip()

    token = uuid.uuid4().hex[:8]
    path = worktrees_root() / _segment(session_id) / token
    branch = f"{BRANCH_PREFIX}{token}"
    with _create_lock:
        _make_room(max_checkouts)
        path.parent.mkdir(parents=True, exist_ok=True)
        result = _git(repo_root, "worktree", "add", "-q", "-b", branch, str(path), base)
        if result.returncode != 0:
            _drop_empty_parent(path)
            raise WorktreeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")
        with _active_lock:
            _active.add(str(path))
    return ChildWorktree(path=path, branch=branch, repo_root=repo_root, base=base)


def _commit_all(path: Path, message: str) -> None:
    _git(path, "add", "-A")
    if _git(path, "diff", "--cached", "--quiet").returncode == 0:
        return
    identity = () if _git(path, "config", "user.email").stdout.strip() else _FALLBACK_IDENTITY
    _git(path, *_COMMIT_FLAGS, *identity, "commit", "-q", "--no-verify", "-m", message)


def finalize(wt: ChildWorktree, *, message: str) -> Dict[str, Any]:
    """Commit leftover changes, remove the checkout, keep the branch if it has work."""
    report: Dict[str, Any] = {"branch": None, "commits": 0}
    try:
        if wt.path.is_dir():
            _commit_all(wt.path, message)
        count = _git(wt.repo_root, "rev-list", "--count", f"{wt.base}..{wt.branch}")
        commits = int(count.stdout.strip()) if count.returncode == 0 else 0
        removed = _git(wt.repo_root, "worktree", "remove", "--force", str(wt.path))
        if removed.returncode != 0 and wt.path.exists():
            shutil.rmtree(wt.path, ignore_errors=True)
            _git(wt.repo_root, "worktree", "prune")
        if commits:
            report.update(branch=wt.branch, commits=commits)
        else:
            _git(wt.repo_root, "branch", "-D", wt.branch)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        report.update(branch=wt.branch, error=f"worktree cleanup failed: {exc}")
    finally:
        with _active_lock:
            _active.discard(str(wt.path))
        _drop_empty_parent(wt.path)
    return report
