"""Memory flush before compaction (plan item 2.1, after OpenClaw memory-core).

Compaction is the moment context is lost. When ``memory.flush_before_compaction``
is on, ``flush_before_compaction`` runs the existing memory miner
(``agent/memory_miner.py``) over the part of the conversation that has not
been flushed yet, writing durable facts to MEMORY.md / USER.md (and, through
``MemoryStore.add``, to the daily journal) *before* the compressor summarises.

Invariants:

* Off by default; also skipped when the agent has no memory or autonomy is
  frozen.
* Cache-safe: it writes files only, through a fresh ``MemoryStore`` loaded
  from disk, so the live agent's injected memory snapshot and the
  conversation are untouched. Nothing is appended to the transcript.
* Never blocks compaction: every failure is returned, not raised.
* Each message is mined at most once per agent (fingerprints are kept on the
  agent), so repeated compactions do not re-send or duplicate facts; the
  miner's own duplicate check and ``MemoryStore.add`` reject repeats too.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_SKIP_ROLES = {"system"}
_COMPACTION_MARKER = "[CONTEXT COMPACTION]"


def flush_enabled() -> bool:
    from agent.feature_flags import flag_enabled
    return flag_enabled("memory", "flush_before_compaction", default=False)


def _frozen() -> bool:
    try:
        from agent.autonomy_guard import frozen
        return bool(frozen())
    except Exception:
        return False


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def _fingerprint(msg: Dict[str, Any]) -> str:
    raw = f"{msg.get('role')}\x00{msg.get('tool_call_id') or ''}\x00{_text(msg.get('content'))}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _unflushed(agent: Any, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = getattr(agent, "_memory_flush_seen", None)
    if not isinstance(seen, set):
        seen = set()
    out = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") in _SKIP_ROLES:
            continue
        if _text(msg.get("content")).lstrip().startswith(_COMPACTION_MARKER):
            continue   # a previous summary, already distilled
        if _fingerprint(msg) in seen:
            continue
        out.append(msg)
    return out


def _mark_flushed(agent: Any, messages: List[Dict[str, Any]]) -> None:
    seen = getattr(agent, "_memory_flush_seen", None)
    if not isinstance(seen, set):
        seen = set()
    seen.update(_fingerprint(m) for m in messages)
    try:
        agent._memory_flush_seen = seen
    except Exception:
        pass


def flush_before_compaction(
    agent: Any,
    messages: List[Dict[str, Any]],
    *,
    llm_caller: Optional[Callable[..., Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Mine durable facts from not-yet-flushed ``messages`` into memory.

    Returns None when the flush does not apply (disabled, no memory, frozen),
    ``{"skipped": True, ...}`` when there is nothing new, otherwise the
    miner's summary (``added`` / ``skipped_duplicates`` / ``error``).
    """
    try:
        if not flush_enabled():
            return None
        if getattr(agent, "_memory_store", None) is None or _frozen():
            return None
        span = _unflushed(agent, messages or [])
        if not span:
            return {"skipped": True, "added": {"user": 0, "memory": 0}, "error": None}

        from agent.memory_miner import mine_session_memory
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.load_from_disk()
        result = mine_session_memory(span, store, llm_caller=llm_caller)
        if not result.get("error"):
            _mark_flushed(agent, span)
        added = result.get("added") or {}
        if added.get("user") or added.get("memory"):
            logger.info("memory flush before compaction: %s user / %s memory facts saved",
                        added.get("user", 0), added.get("memory", 0))
        return result
    except Exception as exc:
        logger.debug("memory flush before compaction failed: %s", exc)
        return {"skipped": False, "added": {"user": 0, "memory": 0}, "error": str(exc)}
