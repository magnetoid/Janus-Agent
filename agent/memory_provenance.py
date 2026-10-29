"""Provenance for built-in memory entries (plan item 3.4, after OpenClaw Memory Wiki).

Every successful ``MemoryStore`` add / replace / remove records one row in a
profile-scoped sidecar database (``memories/provenance.db``): which target,
the entry text and its hash, who wrote it (``source``: ``agent``, ``miner``,
``reconcile``, …), the session, an optional confidence, the hash of the entry
it ``supersedes`` (for replaces) and whether the writing context contained
untrusted external content. ``MEMORY.md`` / ``USER.md`` stay plain text; the
prompt never sees this data, so prompt caching is unaffected.

Callers describe the write with :func:`provenance_context`; ``MemoryStore``
calls :func:`record`. Off unless ``memory.provenance`` is true; never raises.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

UNTRUSTED_MARKER = "<untrusted-content>"

_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "janus_memory_provenance", default=None)
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    action TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    session_id TEXT,
    confidence REAL,
    supersedes TEXT,
    untrusted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provenance_entry ON provenance(target, content_hash, id DESC);
"""


def enabled() -> bool:
    from agent.feature_flags import flag_enabled
    return flag_enabled("memory", "provenance", default=False)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8", "replace")).hexdigest()[:24]


def _db_path() -> Path:
    from tools.memory_tool import get_memory_dir
    return get_memory_dir() / "provenance.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.executescript(_SCHEMA)
    return conn


@contextlib.contextmanager
def provenance_context(source: str, *, session_id: Optional[str] = None,
                       untrusted: bool = False,
                       confidence: Optional[float] = None) -> Iterator[None]:
    """Describe the memory writes made inside this block."""
    token = _ctx.set({"source": source, "session_id": session_id,
                      "untrusted": bool(untrusted), "confidence": confidence})
    try:
        yield
    finally:
        _ctx.reset(token)


def turn_has_untrusted(messages: Optional[List[Dict[str, Any]]]) -> bool:
    """True when a message after the last user turn carries fenced untrusted content."""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            return False
        content = msg.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if UNTRUSTED_MARKER in text:
            return True
    return False


def transcript_has_untrusted(messages: Optional[List[Dict[str, Any]]]) -> bool:
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if UNTRUSTED_MARKER in (content if isinstance(content, str) else str(content or "")):
            return True
    return False


def record(target: str, action: str, content: str, *, supersedes_text: Optional[str] = None) -> None:
    """Store provenance for one successful memory mutation. Never raises."""
    try:
        if not enabled():
            return
        ctx = _ctx.get() or {}
        row = (
            target, action, content_hash(content), (content or "").strip(),
            ctx.get("source") or "unknown", ctx.get("session_id"),
            ctx.get("confidence"),
            content_hash(supersedes_text) if supersedes_text else None,
            1 if ctx.get("untrusted") else 0, time.time(),
        )
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO provenance (target, action, content_hash, content, source, "
                    "session_id, confidence, supersedes, untrusted, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", row)
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        logger.debug("memory provenance record failed: %s", exc)


def lookup(target: str, text: str) -> Optional[Dict[str, Any]]:
    """Latest add/replace provenance for an entry's exact text, or None."""
    try:
        if not _db_path().exists():
            return None
        with _lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT source, session_id, confidence, supersedes, untrusted, created_at, action "
                    "FROM provenance WHERE target = ? AND content_hash = ? "
                    "AND action IN ('add', 'replace') ORDER BY id DESC LIMIT 1",
                    (target, content_hash(text)),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return {"source": row[0], "session_id": row[1], "confidence": row[2],
                "supersedes": row[3], "untrusted": bool(row[4]), "created_at": row[5],
                "action": row[6]}
    except Exception as exc:
        logger.debug("memory provenance lookup failed: %s", exc)
        return None
