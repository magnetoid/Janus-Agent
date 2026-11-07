"""Per-child git worktrees for ``delegate_task(isolation="worktree")``.

Each child gets ``git worktree add -b janus/delegate/<id>`` of the parent's
HEAD under ``<janus_home>/worktrees/<session>/<child>``. When the child is
done, its uncommitted changes are committed to that branch, the checkout is
removed, and the branch is kept only if it holds work. The parent gets the
branch name back and merges or discards it. If the changes cannot be
committed, the checkout is kept and the report says where it is.

``max_checkouts`` caps live checkouts across all sessions and processes
sharing the Janus home (admission takes a file lock). Each checkout has a
``<child>.owner`` file naming its process; a checkout whose owner is gone
is evicted to make room once its work is committed to its branch.
Checkouts of running children, in this or any other live process, never are.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

DEFAULT_MAX_CHECKOUTS = 8
BRANCH_PREFIX = "janus/delegate/"

_GIT_TIMEOUT = 60
# Automatic commits must not stop on the user's hooks or signing setup.
_COMMIT_FLAGS = ("-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}")
_FALLBACK_IDENTITY = ("-c", "user.name=Janus", "-c", "user.email=janus@localhost")

_active: set[str] = set()
_active_lock = threading.Lock()
# Serialises the cap check with the checkout it admits between threads of
# this process (nested orchestrators); _admission_lock covers other processes.
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


@contextmanager
def _admission_lock() -> Iterator[None]:
    root = worktrees_root()
    root.mkdir(parents=True, exist_ok=True)
    with _create_lock, open(root / ".lock", "a+", encoding="utf-8") as fd:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


def _owner_file(path: Path) -> Path:
    return path.with_name(path.name + ".owner")


def _pid_alive(pid: int) -> bool:
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(int(pid)))
    except Exception:
        return True  # fail closed: never evict a checkout we cannot check


def _abandoned(path: Path) -> bool:
    """True when no live process owns *path*."""
    with _active_lock:
        if str(path) in _active:
            return False
    try:
        pid = int(json.loads(_owner_file(path).read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return True  # owner files are written under the admission lock
    return pid == os.getpid() or not _pid_alive(pid)


def _checkouts() -> list[Path]:
    root = worktrees_root()
    if not root.is_dir():
        return []
    return [p for session in root.iterdir() if session.is_dir() for p in session.iterdir() if p.is_dir()]


def _make_room(max_checkouts: int) -> None:
    existing = _checkouts()
    leftovers = sorted((p for p in existing if _abandoned(p)), key=lambda p: p.stat().st_mtime)
    for victim in leftovers:
        if len(existing) < max_checkouts:
            break
        if _evict(victim):
            existing.remove(victim)
    if len(existing) >= max_checkouts:
        raise WorktreeError(
            f"all {max_checkouts} subagent worktrees are in use (delegation.worktree.max_checkouts); "
            "wait for a child to finish or raise the cap"
        )


def _forget(path: Path) -> None:
    with _active_lock:
        _active.discard(str(path))
    try:
        _owner_file(path).unlink()
    except OSError:
        pass
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _evict(path: Path) -> bool:
    """Save a leftover checkout's work on its branch, then remove it.

    Returns False (and leaves the checkout alone) when the work cannot be
    saved.
    """
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode == 0 and common.stdout.strip():
        if not _commit_all(path, "janus: save work from an abandoned subagent worktree"):
            return False
        repo = Path(common.stdout.strip())
        repo = repo.parent if repo.name == ".git" else repo
        _git(repo, "worktree", "remove", "--force", str(path))
        _git(repo, "worktree", "prune")
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _forget(path)
    return True


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
    with _admission_lock():
        _make_room(max_checkouts)
        path.parent.mkdir(parents=True, exist_ok=True)
        result = _git(repo_root, "worktree", "add", "-q", "-b", branch, str(path), base)
        if result.returncode != 0:
            _forget(path)
            raise WorktreeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")
        _owner_file(path).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        with _active_lock:
            _active.add(str(path))
    return ChildWorktree(path=path, branch=branch, repo_root=repo_root, base=base)


def _commit_all(path: Path, message: str) -> bool:
    """Commit everything in *path*; False when that did not work."""
    if _git(path, "add", "-A").returncode != 0:
        return False
    staged = _git(path, "diff", "--cached", "--quiet").returncode
    if staged == 0:
        return True
    if staged != 1:
        return False
    identity = () if _git(path, "config", "user.email").stdout.strip() else _FALLBACK_IDENTITY
    return _git(path, *_COMMIT_FLAGS, *identity, "commit", "-q", "--no-verify", "-m", message).returncode == 0


def finalize(wt: ChildWorktree, *, message: str) -> Dict[str, Any]:
    """Commit leftover changes, remove the checkout, keep the branch if it has work."""
    report: Dict[str, Any] = {"branch": None, "commits": 0}
    try:
        if wt.path.is_dir() and not _commit_all(wt.path, message):
            report.update(
                branch=wt.branch,
                kept_checkout=str(wt.path),
                error=f"could not commit the child's changes; its checkout was kept at {wt.path}",
            )
            return report
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
        # A kept checkout loses its owner, so a later cap check may retry
        # saving it (and removes it only once that works).
        _forget(wt.path)
    return report
