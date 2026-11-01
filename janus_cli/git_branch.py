"""Current git branch for the CLI status bar.

Reads ``.git/HEAD`` directly instead of spawning ``git`` so the status bar
can call it on every repaint (through ``cached_git_branch``) at no cost.
Linked worktrees, whose ``.git`` is a ``gitdir:`` file, are followed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional


def _git_dir_for(start: Path) -> Optional[Path]:
    for candidate in (start, *start.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                git_dir = Path(text[len("gitdir:"):].strip())
                if not git_dir.is_absolute():
                    git_dir = candidate / git_dir
                return git_dir
            return None
    return None


def current_git_branch(start: str | Path) -> Optional[str]:
    """Return the branch checked out at *start*, a short sha when detached, or None."""
    try:
        git_dir = _git_dir_for(Path(start).resolve())
        if git_dir is None:
            return None
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return head[:7] or None


def cached_git_branch(
    start: str | Path,
    *,
    cache: dict,
    ttl: float = 10.0,
    now: Callable[[], float] = time.monotonic,
) -> Optional[str]:
    """``current_git_branch`` memoised in *cache* for *ttl* seconds per path."""
    key = str(start)
    stamp = now()
    hit = cache.get(key)
    if hit is not None and stamp - hit[0] < ttl:
        return hit[1]
    branch = current_git_branch(start)
    cache[key] = (stamp, branch)
    return branch
