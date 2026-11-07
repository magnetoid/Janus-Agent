"""Release channels for ``janus update``.

``main`` follows the tip of a branch (the historical behaviour). ``stable``
follows the newest release tag that ``scripts/release.py`` publishes
(CalVer, ``vYYYY.M.D`` with an optional ``.N`` same-day suffix) and checks
it out as a detached HEAD. Moving to a release older than the current
checkout is a rollback and needs ``--allow-rollback``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Literal, Optional

CHANNELS = ("main", "stable")
DEFAULT_CHANNEL = "main"

_RELEASE_TAG = re.compile(r"^v(\d{4})\.(\d+)\.(\d+)(?:\.(\d+))?$")
_GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")

Move = Literal["current", "forward", "rollback"]


def configured_channel(config: Optional[dict]) -> str:
    """The channel saved in ``updates.channel``, or ``main`` when unset or unknown."""
    updates = (config or {}).get("updates")
    value = updates.get("channel") if isinstance(updates, dict) else None
    value = str(value or "").strip().lower()
    return value if value in CHANNELS else DEFAULT_CHANNEL


def resolve_channel(flag: Optional[str], branch: Optional[str], config: Optional[dict]) -> str:
    """Pick the channel for one ``janus update`` run.

    ``--channel`` wins over the saved channel. ``--branch`` alone means
    "track this branch", so it implies ``main`` for that run.
    """
    if flag is not None:
        channel = flag.strip().lower()
        if channel not in CHANNELS:
            raise ValueError(f"Unknown update channel '{flag}'. Choose one of: {', '.join(CHANNELS)}.")
        if channel == "stable" and branch and branch.strip() not in ("", "main"):
            raise ValueError("--channel stable follows release tags and cannot be combined with --branch.")
        return channel
    if branch and branch.strip():
        return "main"
    return configured_channel(config)


def save_channel(channel: str) -> None:
    """Persist ``updates.channel`` in the user's config.yaml without dumping defaults.

    Raises ``ValueError`` when an existing config.yaml cannot be read or
    parsed, so a broken file is left for the user to repair instead of
    being replaced by ``{"updates": {"channel": ...}}``.
    """
    import yaml

    from janus_cli.config import _set_nested, ensure_janus_home, get_config_path
    from utils import atomic_yaml_write

    path = get_config_path()
    user_config: dict = {}
    if path.exists():
        try:
            user_config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"could not read {path} ({exc.__class__.__name__}); it was left unchanged") from exc
        if not isinstance(user_config, dict):
            raise ValueError(f"{path} is not a mapping; it was left unchanged")
    _set_nested(user_config, "updates.channel", channel)
    ensure_janus_home()
    atomic_yaml_write(path, user_config, sort_keys=False)


def _git(git_cmd: list[str], cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(git_cmd + list(args), cwd=cwd, capture_output=True, text=True)


def latest_release_tag(git_cmd: list[str], cwd: Path) -> Optional[str]:
    """Newest local release tag, or None when nothing has been released yet."""
    result = _git(git_cmd, cwd, "tag", "--list", "v*")
    if result.returncode != 0:
        return None
    best: Optional[tuple[tuple[int, ...], str]] = None
    for name in result.stdout.split():
        match = _RELEASE_TAG.match(name)
        if not match:
            continue
        key = tuple(int(part or 0) for part in match.groups())
        if best is None or key > best[0]:
            best = (key, name)
    return best[1] if best else None


def _commit_of(git_cmd: list[str], cwd: Path, ref: str) -> Optional[str]:
    result = _git(git_cmd, cwd, "rev-parse", f"{ref}^{{commit}}")
    sha = result.stdout.strip() if result.returncode == 0 else ""
    return sha or None


def classify_move(git_cmd: list[str], cwd: Path, tag: str) -> Move:
    """How moving HEAD to *tag* relates to the current checkout.

    ``forward`` only when HEAD is an ancestor of the release; anything else
    (HEAD newer, or diverged) would drop commits from the working tree and
    counts as a rollback.
    """
    head = _git(git_cmd, cwd, "rev-parse", "HEAD").stdout.strip()
    target = _commit_of(git_cmd, cwd, tag)
    if head and head == target:
        return "current"
    if _git(git_cmd, cwd, "merge-base", "--is-ancestor", "HEAD", tag).returncode == 0:
        return "forward"
    # Installer clones are shallow (--depth 1), so the walk from a newer
    # release back to HEAD can stop at the shallow edge. Fetch the full
    # history once and ask again before calling this a rollback.
    if _git(git_cmd, cwd, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        _git(git_cmd, cwd, "fetch", "--quiet", "--unshallow", "origin")
        if _git(git_cmd, cwd, "merge-base", "--is-ancestor", "HEAD", tag).returncode == 0:
            return "forward"
    return "rollback"


def commits_between(git_cmd: list[str], cwd: Path, old: str, new: str) -> Optional[int]:
    result = _git(git_cmd, cwd, "rev-list", "--count", f"{old}..{new}")
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def release_notes_url(origin_url: Optional[str], tag: str) -> Optional[str]:
    """GitHub release page for *tag* on the origin repo, or None off GitHub."""
    match = _GITHUB_REMOTE.search(origin_url or "")
    if not match:
        return None
    owner, repo = match.groups()
    return f"https://github.com/{owner}/{repo}/releases/tag/{tag}"
