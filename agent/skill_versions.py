"""Per-skill version history with undo (plan item 2.5).

Every mutation of an existing skill by the agent's own machinery
(``skill_manage`` edit/patch/write_file/remove_file/delete, and
``self_improve`` in-place promotions) first copies the skill directory into
``<janus_home>/skills/.versions/<name>/<version_id>/``. The newest
``skills.versions.max_versions`` copies are "active"; older ones are moved to
``.versions/<name>/.archive/`` — nothing is ever deleted. ``restore_version``
records the current state as a new version before restoring, so an undo can
itself be undone.

Off by default (``skills.versions.enabled: false``). The whole-tree tarballs
in ``agent/curator_backup.py`` are unchanged; this is the per-skill,
user-facing layer on top.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from janus_constants import get_janus_home

logger = logging.getLogger(__name__)

DEFAULT_MAX_VERSIONS = 10
_INDEX = "index.json"
_lock = threading.Lock()


# -- configuration -----------------------------------------------------------

def _settings() -> Dict[str, Any]:
    try:
        from janus_cli.config import load_config_readonly
        skills = load_config_readonly().get("skills") or {}
        section = skills.get("versions") if isinstance(skills, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def is_enabled() -> bool:
    return bool(_settings().get("enabled"))


def _max_versions() -> int:
    try:
        return max(1, int(_settings().get("max_versions") or DEFAULT_MAX_VERSIONS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_VERSIONS


def _is_pinned(name: str) -> bool:
    try:
        from tools import skill_usage
        return bool(skill_usage.get_record(name).get("pinned"))
    except Exception:
        return False


# -- storage -----------------------------------------------------------------

def _versions_root() -> Path:
    return get_janus_home() / "skills" / ".versions"


def _skill_root(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"invalid skill name {name!r}")
    return _versions_root() / name


def _load_index(name: str) -> List[Dict[str, Any]]:
    path = _skill_root(name) / _INDEX
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_index(name: str, entries: List[Dict[str, Any]]) -> None:
    root = _skill_root(name)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (_INDEX + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(root / _INDEX)


def _hashes(skill_dir: Path) -> Dict[str, str]:
    out = {}
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(skill_dir).as_posix())] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _version_dir(name: str, entry: Dict[str, Any]) -> Path:
    root = _skill_root(name)
    return (root / ".archive" / entry["id"]) if entry.get("archived") else (root / entry["id"])


def _new_id(existing: List[Dict[str, Any]]) -> str:
    vid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    taken = {e["id"] for e in existing}
    n = 1
    base = vid
    while vid in taken:
        vid = f"{base}-{n}"
        n += 1
    return vid


# -- public API --------------------------------------------------------------

def record_version(skill_dir: Path, *, reason: str, actor: str) -> Optional[str]:
    """Copy ``skill_dir`` into the history before it is mutated.

    Returns the version id (the latest one when the content is unchanged), or
    None when history is disabled or there is nothing to record. Never raises:
    a history failure must not block the mutation it precedes.
    """
    try:
        if not is_enabled():
            return None
        skill_dir = Path(skill_dir)
        if not (skill_dir / "SKILL.md").is_file():
            return None
        name = skill_dir.name
        with _lock:
            entries = _load_index(name)
            hashes = _hashes(skill_dir)
            active = [e for e in entries if not e.get("archived")]
            if active and active[-1].get("files") == hashes:
                return active[-1]["id"]
            vid = _new_id(entries)
            dest = _skill_root(name) / vid
            shutil.copytree(skill_dir, dest, symlinks=False)
            entries.append({
                "id": vid,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": reason,
                "actor": actor,
                "path": str(skill_dir.resolve()),
                "files": hashes,
            })
            _enforce_cap(name, entries)
            _save_index(name, entries)
            return vid
    except Exception as exc:
        logger.warning("skill history: could not record %s: %s", skill_dir, exc)
        return None


def _enforce_cap(name: str, entries: List[Dict[str, Any]]) -> None:
    active = [e for e in entries if not e.get("archived")]
    excess = len(active) - _max_versions()
    if excess <= 0:
        return
    archive = _skill_root(name) / ".archive"
    archive.mkdir(parents=True, exist_ok=True)
    for entry in active[:excess]:
        src = _skill_root(name) / entry["id"]
        if src.exists():
            shutil.move(str(src), str(archive / entry["id"]))
        entry["archived"] = True


def list_versions(name: str, *, include_archived: bool = False) -> List[Dict[str, Any]]:
    """Versions of ``name``, newest first (``files`` omitted for brevity)."""
    try:
        entries = _load_index(name)
    except ValueError:
        return []
    rows = [dict(e) for e in entries if include_archived or not e.get("archived")]
    for row in rows:
        row["file_count"] = len(row.pop("files", {}) or {})
    return list(reversed(rows))


def _find(name: str, version_id: Optional[str]) -> Optional[Dict[str, Any]]:
    entries = _load_index(name)
    if version_id is None:
        active = [e for e in entries if not e.get("archived")]
        return active[-1] if active else None
    for entry in entries:
        if entry["id"] == version_id or entry["id"].startswith(version_id):
            return entry
    return None


def version_files(name: str, version_id: str) -> Dict[str, bytes]:
    """The files stored for one version (empty when unknown)."""
    entry = _find(name, version_id)
    if not entry:
        return {}
    d = _version_dir(name, entry)
    if not d.is_dir():
        return {}
    return {str(p.relative_to(d).as_posix()): p.read_bytes()
            for p in sorted(d.rglob("*")) if p.is_file()}


def _current_dir(name: str, entry: Dict[str, Any]) -> Path:
    try:
        from tools.skill_manager_tool import _find_skill
        found = _find_skill(name)
        if found:
            return Path(found["path"])
    except Exception:
        pass
    return Path(entry["path"])


def diff_version(name: str, version_id: Optional[str] = None) -> str:
    """Unified diff from a version (default: newest) to the skill as it is now."""
    entry = _find(name, version_id)
    if not entry:
        return ""
    old = version_files(name, entry["id"])
    cur_dir = _current_dir(name, entry)
    new = ({str(p.relative_to(cur_dir).as_posix()): p.read_bytes()
            for p in sorted(cur_dir.rglob("*")) if p.is_file()} if cur_dir.is_dir() else {})
    chunks: List[str] = []
    for rel in sorted(set(old) | set(new)):
        a = old.get(rel, b"").decode("utf-8", "replace").splitlines(keepends=True)
        b = new.get(rel, b"").decode("utf-8", "replace").splitlines(keepends=True)
        if a == b:
            continue
        chunks.extend(difflib.unified_diff(a, b, fromfile=f"{entry['id']}/{rel}",
                                           tofile=f"current/{rel}"))
    return "".join(chunks)


def restore_version(name: str, version_id: Optional[str] = None, *,
                    force: bool = False) -> Tuple[bool, str]:
    """Put a skill back to a recorded version (default: the newest one).

    The current state is recorded first, so nothing is lost. A pinned skill
    is only restored with ``force=True``.
    """
    entry = _find(name, version_id)
    if not entry:
        return False, f"No recorded version {version_id or ''} for skill '{name}'.".replace("  ", " ")
    if _is_pinned(name) and not force:
        return False, (f"Skill '{name}' is pinned; pass --force to restore it anyway "
                       f"(or `janus curator unpin {name}`).")
    src = _version_dir(name, entry)
    if not src.is_dir():
        return False, f"Version {entry['id']} of '{name}' is missing on disk."
    target = _current_dir(name, entry)
    if target.is_dir():
        record_version(target, reason=f"before restore of {entry['id']}", actor="restore")
    staging = Path(tempfile.mkdtemp(prefix=f".restore-{name}-", dir=str(target.parent)
                                    if target.parent.exists() else None))
    try:
        shutil.copytree(src, staging / name)
        if target.exists():
            shutil.rmtree(target)       # its content was just recorded above
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging / name), str(target))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass
    return True, f"Restored '{name}' to version {entry['id']}."
