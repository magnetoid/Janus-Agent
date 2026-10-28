"""Durable background delegation (plan item 2.3).

``delegate_task(background=true)`` hands the child to a
:class:`BackgroundDelegationManager` instead of blocking the parent turn:

* the handle is written to ``SessionDB.delegations`` *before* the child
  starts, so the record survives even if the process dies mid-run;
* the child runs on a daemon thread with the parent's ``contextvars``
  copied, so gateway routing (``JANUS_SESSION_*``) still resolves inside
  the child's tools;
* the child's transcript streams to its own session row as usual
  (``sessions.parent_session_id``), so ``janus delegations attach`` can
  show it live;
* on completion the outcome is folded into the row and a
  ``delegation_complete`` event is emitted. With no sink registered the
  event goes onto ``tools.process_registry.completion_queue``, which the
  CLI and TUI already drain into the live session; the gateway registers
  a sink and injects an internal message routed by the stored ``route``.
* ``janus delegations stop`` from *another* process only has the
  database: it sets ``stop_requested`` and the owning manager's poller
  interrupts the child.

A process restart cannot resume a half-run child (the agent loop is not
re-entrant), so ``reconcile_orphans`` marks rows whose worker pid is gone
as ``orphaned`` and announces them once, pointing at the child session
that holds whatever transcript was produced.

Background children are deliberately **not** in ``parent._active_children``:
a parent interrupt, ``/new`` or gateway cache eviction must not kill them.
"""
from __future__ import annotations

import contextvars
import logging
import os
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_RUNNING = 3
DEFAULT_POLL_INTERVAL_S = 5.0
SUMMARY_LIMIT = 4000

_STATUS_MAP = {
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
    "timeout": "timeout",
    "interrupted": "interrupted",
}


class BackgroundLimitError(RuntimeError):
    """Raised by :meth:`BackgroundDelegationManager.start` at the running cap."""


def _default_queue() -> "queue.Queue":
    from tools.process_registry import process_registry
    return process_registry.completion_queue


def _pid_alive_default(pid: int) -> bool:
    try:
        from gateway.status import _pid_exists
        return bool(_pid_exists(int(pid)))
    except Exception:
        return True   # fail closed: never orphan a row we cannot check


def _as_str(value: Any) -> Optional[str]:
    """Only real strings go into the row (test doubles expose MagicMock attributes)."""
    return value if isinstance(value, str) and value else None


def _clip(text: Any, limit: int = SUMMARY_LIMIT) -> str:
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n[… truncated, {len(s) - limit} more chars in the child session]"


def format_delegation_notification(evt: Dict[str, Any]) -> str:
    """Render a delegation event as the ``[IMPORTANT: …]`` turn the parent sees."""
    did = evt.get("delegation_id") or evt.get("session_id") or "?"
    goal = _clip(evt.get("goal", ""), 300)
    child = evt.get("child_session_id") or "?"
    status = evt.get("status", "?")
    if evt.get("type") == "delegation_orphaned":
        return (
            f"[IMPORTANT: Background delegation {did} was lost: the process running it "
            f"exited before it finished.\nGoal: {goal}\n"
            f"Whatever it produced is in session {child} "
            f"(`janus delegations attach {did}`). Re-delegate if the work still matters.]"
        )
    body = _clip(evt.get("summary") or "")
    error = evt.get("error")
    lines = [f"[IMPORTANT: Background delegation {did} {status}.", f"Goal: {goal}"]
    if error:
        lines.append(f"Error: {_clip(error, 500)}")
    lines.append(f"Transcript: session {child} (`janus delegations attach {did}`)")
    lines.append("Result:")
    lines.append(body + "]")
    return "\n".join(lines)


class BackgroundDelegationManager:
    """Owns the worker threads for background children in this process."""

    def __init__(
        self,
        *,
        db: Any,
        sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_running: int = DEFAULT_MAX_RUNNING,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        pid: Optional[int] = None,
    ):
        self._db = db
        self._sink = sink
        self._max_running = max(1, int(max_running))
        self._poll_interval = max(0.01, float(poll_interval))
        self._clock = clock
        self._pid = os.getpid() if pid is None else int(pid)
        self._lock = threading.Lock()
        self._running: Dict[str, Dict[str, Any]] = {}
        self._poller: Optional[threading.Thread] = None

    # -- configuration -------------------------------------------------

    def set_sink(self, sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._sink = sink

    @property
    def db(self) -> Any:
        return self._db

    # -- queries ---------------------------------------------------------

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def owns(self, delegation_id: str) -> bool:
        with self._lock:
            return delegation_id in self._running

    # -- lifecycle -------------------------------------------------------

    def start(
        self,
        *,
        child: Any,
        goal: str,
        parent_agent: Any,
        run_child: Callable[[], Dict[str, Any]],
        role: Optional[str] = None,
        model: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record the handle, start the worker thread, return the handle."""
        with self._lock:
            if len(self._running) >= self._max_running:
                raise BackgroundLimitError(
                    f"{len(self._running)} background delegation(s) already running "
                    f"(delegation.background.max_running={self._max_running})"
                )
            child_session_id = _as_str(getattr(child, "session_id", None))
            did = self._db.create_delegation(
                goal=goal,
                parent_session_id=_as_str(getattr(parent_agent, "session_id", None)),
                child_session_id=child_session_id,
                subagent_id=_as_str(getattr(child, "_subagent_id", None)),
                role=_as_str(role),
                model=_as_str(model),
                route=dict(route or {}),
                pid=self._pid,
                now=self._clock(),
            )
            record = {
                "id": did, "child": child, "parent": parent_agent, "goal": goal,
                "route": dict(route or {}), "child_session_id": child_session_id,
                "stop_sent": False,
            }
            self._running[did] = record
            ctx = contextvars.copy_context()
            thread = threading.Thread(
                target=ctx.run,
                args=(self._worker, record, run_child),
                name=f"bg-delegation-{did}",
                daemon=True,
            )
            record["thread"] = thread
            thread.start()
            self._ensure_poller_locked()
        return {
            "id": did,
            "status": "running",
            "child_session_id": child_session_id,
            "goal": goal,
        }

    def request_stop(self, delegation_id: str) -> bool:
        """Interrupt a child this process owns; always flags the row too."""
        flagged = bool(self._db.request_delegation_stop(delegation_id, now=self._clock()))
        with self._lock:
            record = self._running.get(delegation_id)
        if record is None:
            return flagged
        self._interrupt(record, "stopped by `janus delegations stop`")
        return True

    def reconcile_orphans(
        self,
        *,
        pid_alive: Callable[[int], bool] = _pid_alive_default,
        announce: bool = True,
    ) -> List[Dict[str, Any]]:
        """Mark rows owned by dead processes orphaned; return the rows changed.

        With ``announce`` (the gateway / an in-process manager) every orphaned
        row that nobody has been told about yet is emitted once and marked
        delivered, including rows a quiet caller orphaned earlier. A quiet
        caller (``janus delegations list``, ``announce=False``) only changes
        the status, so the one announcement a lost delegation gets is kept
        for a surface that can actually reach the parent.
        """
        dead = [p for p in self._db.running_delegation_pids()
                if p != self._pid and not pid_alive(p)]
        rows = self._db.orphan_delegations(pids=dead, now=self._clock()) if dead else []
        if announce:
            for row in self._db.list_delegations(status="orphaned", limit=1000):
                if row.get("delivered_at") is None:
                    self._emit(self._event("delegation_orphaned", row))
                    self._db.mark_delegation_delivered(row["id"], now=self._clock())
        return rows

    # -- internals -------------------------------------------------------

    def _interrupt(self, record: Dict[str, Any], message: str) -> None:
        if record.get("stop_sent"):
            return
        record["stop_sent"] = True
        try:
            record["child"].interrupt(message)
        except TypeError:
            record["child"].interrupt()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("background delegation %s: interrupt failed: %s", record["id"], exc)

    def _ensure_poller_locked(self) -> None:
        if self._poller is not None and self._poller.is_alive():
            return
        self._poller = threading.Thread(
            target=self._poll_stop_requests, name="bg-delegation-poller", daemon=True,
        )
        self._poller.start()

    def _poll_stop_requests(self) -> None:
        """Honour ``stop_requested`` set by another process while children run."""
        while True:
            time.sleep(self._poll_interval)
            with self._lock:
                records = [r for r in self._running.values() if not r.get("stop_sent")]
                if not self._running:
                    self._poller = None
                    return
            for record in records:
                try:
                    row = self._db.get_delegation(record["id"])
                except Exception as exc:
                    logger.debug("background delegation poll failed: %s", exc)
                    continue
                if row and row.get("stop_requested"):
                    self._interrupt(record, "stopped by `janus delegations stop`")

    def _worker(self, record: Dict[str, Any], run_child: Callable[[], Dict[str, Any]]) -> None:
        did = record["id"]
        entry: Dict[str, Any]
        try:
            entry = run_child() or {}
        except BaseException as exc:  # noqa: BLE001 - the thread must always settle the row
            logger.warning("background delegation %s crashed: %s", did, exc)
            entry = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        try:
            self._finish(record, entry)
        finally:
            with self._lock:
                self._running.pop(did, None)

    def _finish(self, record: Dict[str, Any], entry: Dict[str, Any]) -> None:
        did = record["id"]
        status = _STATUS_MAP.get(str(entry.get("status", "error")), "failed")
        cost = entry.get("_child_cost_usd")
        try:
            cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        self._db.finish_delegation(
            did,
            status=status,
            summary=entry.get("summary"),
            error=entry.get("error"),
            api_calls=entry.get("api_calls"),
            cost_usd=cost,
            now=self._clock(),
        )
        parent = record.get("parent")
        if cost and parent is not None:
            try:
                current = float(getattr(parent, "session_estimated_cost_usd", 0.0) or 0.0)
                parent.session_estimated_cost_usd = current + cost
                if hasattr(parent, "session_cost_source"):
                    parent.session_cost_source = "subagent"
            except Exception:
                pass
        row = self._db.get_delegation(did) or {
            "id": did, "goal": record["goal"], "status": status,
            "summary": entry.get("summary"), "error": entry.get("error"),
            "child_session_id": record.get("child_session_id"), "route": record["route"],
        }
        self._emit(self._event("delegation_complete", row))
        self._db.mark_delegation_delivered(did, now=self._clock())

    @staticmethod
    def _event(kind: str, row: Dict[str, Any]) -> Dict[str, Any]:
        route = row.get("route") or {}
        evt = {
            "type": kind,
            # ``session_id`` is what the CLI/TUI pollers use as the event's
            # identity (dedup, consumed-check); for processes it is the
            # process id, for us it is the delegation id.
            "session_id": row["id"],
            "delegation_id": row["id"],
            "goal": row.get("goal") or "",
            "status": row.get("status"),
            "summary": row.get("summary"),
            "error": row.get("error"),
            "child_session_id": row.get("child_session_id"),
            "parent_session_id": row.get("parent_session_id"),
        }
        for key in ("session_key", "platform", "chat_id", "chat_type", "thread_id",
                    "user_id", "user_name", "message_id"):
            if route.get(key):
                evt[key] = route[key]
        return evt

    def _emit(self, evt: Dict[str, Any]) -> None:
        try:
            if self._sink is not None:
                self._sink(evt)
            else:
                _default_queue().put(evt)
        except Exception as exc:
            logger.warning("background delegation %s: could not announce result: %s",
                           evt.get("delegation_id"), exc)


# ---------------------------------------------------------------------------
# Process-wide manager
# ---------------------------------------------------------------------------

_manager: Optional[BackgroundDelegationManager] = None
_manager_lock = threading.Lock()


def capture_route() -> Dict[str, Any]:
    """Snapshot gateway routing from the calling thread's session context.

    Must run on the parent's thread: ``contextvars`` are not inherited by
    worker threads, and the completion event is emitted long after the
    parent turn (and its context) is gone.
    """
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return {}
    route = {}
    for key, env in (
        ("platform", "JANUS_SESSION_PLATFORM"), ("chat_id", "JANUS_SESSION_CHAT_ID"),
        ("thread_id", "JANUS_SESSION_THREAD_ID"), ("user_id", "JANUS_SESSION_USER_ID"),
        ("user_name", "JANUS_SESSION_USER_NAME"), ("session_key", "JANUS_SESSION_KEY"),
        ("message_id", "JANUS_SESSION_MESSAGE_ID"),
    ):
        try:
            value = get_session_env(env, "")
        except Exception:
            value = ""
        if value:
            route[key] = value
    return route


def get_manager(db: Any = None, *, max_running: Optional[int] = None) -> BackgroundDelegationManager:
    """Return the process-wide manager, creating it on first use."""
    global _manager
    with _manager_lock:
        if _manager is None:
            if db is None:
                from janus_constants import get_janus_home
                from janus_state import SessionDB
                db = SessionDB(db_path=get_janus_home() / "state.db")
            _manager = BackgroundDelegationManager(
                db=db, max_running=max_running or DEFAULT_MAX_RUNNING,
            )
        elif max_running:
            _manager._max_running = max(1, int(max_running))
        return _manager


def set_completion_sink(sink: Optional[Callable[[Dict[str, Any]], None]], db: Any = None) -> BackgroundDelegationManager:
    """Register where completion events go (the gateway does this at start-up)."""
    mgr = get_manager(db)
    mgr.set_sink(sink)
    return mgr


def reset_for_tests() -> None:
    global _manager
    with _manager_lock:
        _manager = None
