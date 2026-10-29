"""Per-skill version history and undo.

Generalises the whole-tree snapshot in :mod:`agent.curator_backup` down to a
single skill. Every writer that mutates a skill directory — ``skill_manage``,
the skill miner, the curator, ``self_improve`` and the user's own CLI verbs —
calls :func:`record_version` *before* the write. The pre-write state of the
skill directory is copied to::

    <janus_home>/skills/.versions/<skill>/<id>/        # byte-exact copy
    <janus_home>/skills/.versions/<skill>/index.json   # ordered metadata

where ``<id>`` is ``<utc-timestamp>-<source>``. ``index.json`` records, per
version, the writer (``source``), a short ``reason``, the time, where the skill
lived (``skill_path``) and whether the skill existed at all (``absent`` — e.g.
the state before a ``create``).

Bounded, archive-not-delete: at most ``skills.versions.keep`` (default 10)
versions per skill stay as plain directories. Older ones are appended to a
single per-skill tarball, ``.versions/<skill>/.archive/versions.tar``, and only
then is the directory removed. The index keeps every entry (flagged
``archived``), so history is never lost and archived versions remain
restorable.

Undo is itself recorded: before restoring, the current state of the skill is
*moved* (not copied, not deleted) into a new ``user`` version, so an undo can
always be undone.

Pinned skills are respected the way the curator respects them: the automatic
and user-initiated mutators here (``undo``, ``archive``) refuse to touch a
pinned skill. Recording a version never modifies the live skill.

``.versions`` is in :data:`agent.skill_utils.EXCLUDED_SKILL_DIRS`, so version
copies are never discovered as live skills, and in curator_backup's exclusion
set, so a curator rollback never rewinds history.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from janus_constants import get_janus_home

logger = logging.getLogger(__name__)

try:  # fcntl is Unix-only; Windows falls back to msvcrt.
    import fcntl
except ImportError:  # pragma: no cover - platform-specific
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]


DEFAULT_KEEP = 10
VERSIONS_DIRNAME = ".versions"
INDEX_FILENAME = "index.json"
ARCHIVE_DIRNAME = ".archive"
ARCHIVE_TARBALL = "versions.tar"

SOURCE_SKILL_MANAGE = "skill_manage"
SOURCE_SKILL_MINER = "skill_miner"
SOURCE_CURATOR = "curator"
SOURCE_SELF_IMPROVE = "self_improve"
SOURCE_USER = "user"
SOURCES = (
    SOURCE_SKILL_MANAGE,
    SOURCE_SKILL_MINER,
    SOURCE_CURATOR,
    SOURCE_SELF_IMPROVE,
    SOURCE_USER,
)

_REASON_MAX = 200
# Skill names are used as directory names under .versions/. Anything that
# could escape that directory (separators, "..", leading dots) is refused.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Writer attribution. A caller that drives skill_manage indirectly (the
# curator's forked review agent) sets this so the versions it causes are
# labelled with the real writer. ContextVars propagate into tool worker
# threads via tools.thread_context.
_attribution: ContextVar[Optional[Tuple[str, str]]] = ContextVar(
    "janus_skill_version_attribution", default=None
)

# In-process serialisation. flock() on separate file descriptors also blocks
# between threads, but the RLock lets nested calls on one thread re-enter
# without deadlocking on the file lock.
_process_lock = threading.RLock()
_held = threading.local()


class SkillVersionError(Exception):
    """Raised by the user-facing operations (undo/archive/restore) on refusal."""


# ---------------------------------------------------------------------------
# Paths + config
# ---------------------------------------------------------------------------

def skills_root() -> Path:
    return get_janus_home() / "skills"


def versions_root() -> Path:
    return skills_root() / VERSIONS_DIRNAME


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name) or ".." in name:
        raise SkillVersionError(f"invalid skill name: {name!r}")
    return name


def _store(name: str) -> Path:
    return versions_root() / _validate_name(name)


def _config() -> Dict[str, Any]:
    try:
        from janus_cli.config import load_config
        cfg = load_config()
    except Exception as e:
        logger.debug("skill_versions: config load failed: %s", e)
        return {}
    skills = cfg.get("skills") if isinstance(cfg, dict) else None
    versions = skills.get("versions") if isinstance(skills, dict) else None
    return versions if isinstance(versions, dict) else {}


def is_enabled() -> bool:
    return bool(_config().get("enabled", True))


def get_keep() -> int:
    try:
        n = int(_config().get("keep", DEFAULT_KEEP))
    except (TypeError, ValueError):
        n = DEFAULT_KEEP
    return max(1, n)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def attributed(source: str, reason: str = "") -> Iterator[None]:
    """Label every version recorded inside this block with *source*."""
    if source not in SOURCES:
        raise ValueError(f"unknown skill version source: {source!r}")
    token = _attribution.set((source, reason))
    try:
        yield
    finally:
        _attribution.reset(token)


def attributed_source(default: str) -> Tuple[str, str]:
    """Return ``(source, context_reason)`` — the active attribution or *default*."""
    current = _attribution.get()
    if current is not None:
        return current
    return default, ""


# ---------------------------------------------------------------------------
# Locking + index IO
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(name: str) -> Iterator[Path]:
    """Exclusive per-skill lock, across threads and processes. Re-entrant."""
    store = _store(name)
    with _process_lock:
        depth: Dict[str, int] = getattr(_held, "depth", None) or {}
        _held.depth = depth
        if depth.get(name):
            depth[name] += 1
            try:
                yield store
            finally:
                depth[name] -= 1
            return
        store.mkdir(parents=True, exist_ok=True)
        lock_path = store / ".lock"
        fd = None
        try:
            if fcntl is not None:
                fd = open(lock_path, "a+", encoding="utf-8")
                fcntl.flock(fd, fcntl.LOCK_EX)
            elif msvcrt is not None:
                if not lock_path.exists() or lock_path.stat().st_size == 0:
                    lock_path.write_text(" ", encoding="utf-8")
                fd = open(lock_path, "r+", encoding="utf-8")
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            depth[name] = 1
            yield store
        finally:
            depth[name] = 0
            if fd is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                fd.close()


def _load_index(store: Path, name: str) -> Dict[str, Any]:
    path = store / INDEX_FILENAME
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("versions"), list):
                return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skill_versions: unreadable index %s (%s); rebuilding", path, e)
        # Preserve the unreadable file rather than overwrite it silently.
        try:
            path.replace(path.with_name(f"{INDEX_FILENAME}.corrupt-{_utc_id()}"))
        except OSError:
            pass
    return {"skill": name, "versions": []}


def _save_index(store: Path, index: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(store), prefix=".index.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, sort_keys=True)
        os.replace(tmp, store / INDEX_FILENAME)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def _utc_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def _new_id(store: Path, index: Dict[str, Any], source: str) -> str:
    taken = {e.get("id") for e in index["versions"]}
    base = f"{_utc_id()}-{source}"
    vid, n = base, 2
    while vid in taken or (store / vid).exists():
        vid = f"{base}-{n}"
        n += 1
    return vid


def _encode_path(path: Path) -> str:
    """Store paths under the local skills root relative to it (portable across
    profile moves); anything else (external skill dirs) absolute."""
    try:
        return path.resolve().relative_to(skills_root().resolve()).as_posix()
    except (ValueError, OSError):
        return str(path.resolve())


def _decode_path(stored: str) -> Path:
    p = Path(stored)
    return p if p.is_absolute() else skills_root() / p


def _tree_stats(root: Path) -> Tuple[int, int]:
    files = size = 0
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            files += 1
            with contextlib.suppress(OSError):
                size += p.stat().st_size
    return files, size


def _copy_tree(src: Path, dest: Path) -> None:
    # symlinks=True: copy links as links, never follow them out of the skill.
    shutil.copytree(src, dest, symlinks=True)


def _record_locked(
    name: str,
    store: Path,
    index: Dict[str, Any],
    skill_dir: Path,
    source: str,
    reason: str,
    *,
    move: bool = False,
    assume_absent: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Snapshot *skill_dir* into a new version. Caller holds the lock and
    saves the index. ``move=True`` moves the live directory into history
    instead of copying it (used by undo, so nothing is ever deleted)."""
    if source not in SOURCES:
        raise ValueError(f"unknown skill version source: {source!r}")
    vid = _new_id(store, index, source)
    dest = store / vid
    absent = assume_absent or not skill_dir.exists()
    try:
        if absent:
            dest.mkdir()
        elif move:
            shutil.move(str(skill_dir), str(dest))
        else:
            _copy_tree(skill_dir, dest)
    except Exception:
        if not move and dest.exists():
            # Our own partial copy — not user data.
            shutil.rmtree(dest, ignore_errors=True)
        raise
    files, size = (0, 0) if absent else _tree_stats(dest)
    entry: Dict[str, Any] = {
        "id": vid,
        "source": source,
        "reason": (reason or "")[:_REASON_MAX],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "skill_path": _encode_path(skill_dir),
        "absent": absent,
        "files": files,
        "bytes": size,
        "archived": False,
    }
    if extra:
        entry.update(extra)
    index["versions"].append(entry)
    return vid


def _archive_overflow(store: Path, index: Dict[str, Any], keep: int) -> List[str]:
    """Move versions beyond the newest *keep* into the per-skill tarball.

    The tarball is opened in append mode and closed before any directory is
    removed, so a failure leaves the directories (and the index) untouched.
    """
    live = [e for e in index["versions"] if not e.get("archived")]
    excess = live[: max(0, len(live) - keep)]
    if not excess:
        return []
    archive_dir = store / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    tar_path = archive_dir / ARCHIVE_TARBALL
    with tarfile.open(tar_path, "a") as tf:
        for e in excess:
            d = store / e["id"]
            if d.exists():
                tf.add(str(d), arcname=e["id"], recursive=True)
            else:
                info = tarfile.TarInfo(e["id"])
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
    archived: List[str] = []
    for e in excess:
        e["archived"] = True
        archived.append(e["id"])
        shutil.rmtree(store / e["id"], ignore_errors=True)
    return archived


@contextlib.contextmanager
def _materialized(store: Path, entry: Dict[str, Any]) -> Iterator[Path]:
    """Yield a directory holding the version's files (extracting from the
    tarball into a scratch dir if the version was archived)."""
    vid = entry["id"]
    if not entry.get("archived"):
        yield store / vid
        return
    tar_path = store / ARCHIVE_DIRNAME / ARCHIVE_TARBALL
    with tempfile.TemporaryDirectory(dir=str(store), prefix=".extract-") as tmp:
        with tarfile.open(tar_path, "r") as tf:
            members = [
                m for m in tf.getmembers()
                if m.name == vid or m.name.startswith(vid + "/")
            ]
            for m in members:
                if m.name.startswith("/") or ".." in Path(m.name).parts:
                    raise SkillVersionError(f"refusing unsafe archive member {m.name!r}")
            try:
                tf.extractall(tmp, members=members, filter="tar")
            except TypeError:  # Python without the extraction-filter backport
                tf.extractall(tmp, members=members)
        out = Path(tmp) / vid
        out.mkdir(exist_ok=True)
        yield out


# ---------------------------------------------------------------------------
# Public API — writers
# ---------------------------------------------------------------------------

def record_version(
    name: str,
    skill_dir: Path,
    *,
    source: str,
    reason: str = "",
) -> Optional[str]:
    """Snapshot the current state of *skill_dir* before a write.

    Best-effort: returns the new version id, or ``None`` when versioning is
    disabled or the snapshot failed. Never raises, so a full disk can never
    block a skill write; never modifies *skill_dir*.
    """
    if not is_enabled():
        return None
    try:
        with _locked(name) as store:
            index = _load_index(store, name)
            vid = _record_locked(name, store, index, Path(skill_dir), source, reason)
            _archive_overflow(store, index, get_keep())
            _save_index(store, index)
            return vid
    except Exception as e:
        logger.warning("skill_versions: could not record version of %s: %s", name, e)
        logger.debug("skill_versions: record failure detail", exc_info=True)
        return None


def record_absent(name: str, skill_dir: Path, *, source: str, reason: str = "") -> Optional[str]:
    """Record that *name* did not exist before a write that has already happened
    (e.g. restoring from the curator archive). Best-effort like record_version."""
    if not is_enabled():
        return None
    try:
        with _locked(name) as store:
            index = _load_index(store, name)
            vid = _record_locked(name, store, index, Path(skill_dir), source, reason,
                                 assume_absent=True)
            _archive_overflow(store, index, get_keep())
            _save_index(store, index)
            return vid
    except Exception as e:
        logger.warning("skill_versions: could not record version of %s: %s", name, e)
        return None


# ---------------------------------------------------------------------------
# Public API — readers
# ---------------------------------------------------------------------------

def list_versions(name: str) -> List[Dict[str, Any]]:
    """Every recorded version of *name*, newest first, with a 1-based ``n``."""
    store = _store(name)
    if not (store / INDEX_FILENAME).exists():
        return []
    index = _load_index(store, name)
    out = []
    for n, e in enumerate(reversed(index["versions"]), start=1):
        row = dict(e)
        row["n"] = n
        out.append(row)
    return out


def resolve_version(name: str, ref: Optional[str] = None) -> Dict[str, Any]:
    """Find a version by full id, unique id prefix, or history position
    (``1`` = newest). ``None`` selects the newest."""
    rows = list_versions(name)
    if not rows:
        raise SkillVersionError(f"no recorded versions for skill '{name}'")
    if ref is None or ref == "":
        return rows[0]
    ref = str(ref).strip()
    if ref.isdigit() and len(ref) <= 4:
        n = int(ref)
        if 1 <= n <= len(rows):
            return rows[n - 1]
        raise SkillVersionError(f"skill '{name}' has {len(rows)} version(s); no #{n}")
    exact = [r for r in rows if r["id"] == ref]
    if exact:
        return exact[0]
    matches = [r for r in rows if r["id"].startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SkillVersionError(f"no version '{ref}' for skill '{name}'")
    raise SkillVersionError(f"version '{ref}' is ambiguous ({len(matches)} matches)")


def _current_dir(name: str, entry: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Where the skill lives right now: the version's recorded path if it still
    exists, else the active skill with this name in any skills root."""
    if entry is not None:
        p = _decode_path(entry["skill_path"])
        if p.exists():
            return p
    try:
        from agent.skill_utils import get_all_skills_dirs, resolve_active_skill_md
        for root in get_all_skills_dirs():
            md = resolve_active_skill_md(root, name)
            if md is not None:
                return md.parent
    except Exception:
        logger.debug("skill_versions: active lookup failed for %s", name, exc_info=True)
    return None


def _read_tree(root: Optional[Path]) -> Dict[str, bytes]:
    if root is None or not root.exists():
        return {}
    out: Dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            out[p.relative_to(root).as_posix()] = f"<symlink → {os.readlink(p)}>".encode("utf-8")
        elif p.is_file():
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def diff(name: str, ref: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """Unified diff from version *ref* (default newest) to the current skill.

    Returns ``(version_entry, diff_text)``; the text is empty when identical.
    """
    entry = resolve_version(name, ref)
    store = _store(name)
    live = _current_dir(name, entry)
    with _materialized(store, entry) as vdir:
        old = {} if entry.get("absent") else _read_tree(vdir)
    new = _read_tree(live)
    chunks: List[str] = []
    for rel in sorted(set(old) | set(new)):
        a, b = old.get(rel), new.get(rel)
        if a == b:
            continue
        from_name = f"{entry['id']}/{rel}" if a is not None else "/dev/null"
        to_name = f"current/{rel}" if b is not None else "/dev/null"
        try:
            a_txt = (a or b"").decode("utf-8")
            b_txt = (b or b"").decode("utf-8")
        except UnicodeDecodeError:
            chunks.append(f"Binary files {from_name} and {to_name} differ\n")
            continue
        chunks.extend(difflib.unified_diff(
            a_txt.splitlines(keepends=True), b_txt.splitlines(keepends=True),
            fromfile=from_name, tofile=to_name,
        ))
        if chunks and not chunks[-1].endswith("\n"):
            chunks[-1] += "\n\\ No newline at end of file\n"
    return entry, "".join(chunks)


# ---------------------------------------------------------------------------
# Public API — user operations
# ---------------------------------------------------------------------------

def is_pinned(name: str) -> bool:
    """Same pin source the curator consults (``.usage.json``)."""
    try:
        from tools import skill_usage
        return bool(skill_usage.get_record(name).get("pinned"))
    except Exception:
        logger.debug("skill_versions: pin lookup failed for %s", name, exc_info=True)
        return False


def _pinned_message(name: str) -> str:
    return (f"skill '{name}' is pinned — unpin first with "
            f"`janus curator unpin {name}`")


def _check_restorable_target(target: Path) -> None:
    """Undo may only write inside a configured skills root, never into history."""
    from agent.skill_utils import get_all_skills_dirs
    try:
        resolved = target.resolve()
    except OSError as e:
        raise SkillVersionError(f"cannot resolve restore target {target}: {e}") from e
    if VERSIONS_DIRNAME in resolved.parts:
        raise SkillVersionError(f"refusing to restore into version history: {target}")
    for root in get_all_skills_dirs():
        try:
            resolved.relative_to(root.resolve())
            return
        except (ValueError, OSError):
            continue
    raise SkillVersionError(f"restore target is outside every skills directory: {target}")


def undo(name: str, ref: Optional[str] = None) -> Dict[str, Any]:
    """Restore *name* to version *ref* (default newest), byte-exact.

    The current state is first moved into a new ``user`` version (so the undo
    is itself undoable — undoing twice is a redo). Refuses pinned skills.
    Returns ``{"restored": <entry>, "recorded": <new id>, "path": <str>}``.
    """
    _validate_name(name)
    if is_pinned(name):
        raise SkillVersionError(_pinned_message(name))
    with _locked(name) as store:
        index = _load_index(store, name)
        entry = resolve_version(name, ref)
        target = _decode_path(entry["skill_path"])
        _check_restorable_target(target)
        live = _current_dir(name, entry)
        if live is not None and live.resolve() != target.resolve() and target.exists():
            raise SkillVersionError(
                f"cannot restore to {target}: it is occupied by something other "
                f"than the current '{name}' skill"
            )
        with _materialized(store, entry) as vdir:
            snap_from = live if live is not None else target
            new_id = _record_locked(
                name, store, index, snap_from, SOURCE_USER,
                f"undo to {entry['id']}", move=True,
                extra={"undo_of": entry["id"]},
            )
            try:
                if not entry.get("absent"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _copy_tree(vdir, target)
            except Exception as e:
                # Put the moved-aside state back; the version dir is ours.
                moved = store / new_id
                if moved.exists() and not snap_from.exists() and any(moved.iterdir()):
                    with contextlib.suppress(OSError):
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.move(str(moved), str(snap_from))
                index["versions"] = [v for v in index["versions"] if v["id"] != new_id]
                _save_index(store, index)
                raise SkillVersionError(f"restore failed (state put back): {e}") from e
        # Archive overflow only after the restore source is no longer needed.
        _archive_overflow(store, index, get_keep())
        _save_index(store, index)
    return {"restored": entry, "recorded": new_id, "path": str(target)}


def archive(name: str) -> str:
    """Move *name* into ``skills/.archive/`` (the curator's archive), recording
    a version first. Refuses pinned skills, like ``janus curator archive``."""
    _validate_name(name)
    if is_pinned(name):
        raise SkillVersionError(_pinned_message(name))
    from tools import skill_usage
    with attributed(SOURCE_USER, "janus skills archive"):
        ok, msg = skill_usage.archive_skill(name)
    if not ok:
        raise SkillVersionError(msg)
    return msg


def restore(name: str) -> str:
    """Bring *name* back from ``skills/.archive/``; recorded as a version."""
    _validate_name(name)
    from tools import skill_usage
    with attributed(SOURCE_USER, "janus skills restore"):
        ok, msg = skill_usage.restore_skill(name)
    if not ok:
        raise SkillVersionError(msg)
    return msg


def format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
