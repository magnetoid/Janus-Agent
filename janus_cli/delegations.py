"""``janus delegations`` — inspect and control background delegate_task children.

Reads the ``delegations`` table in ``state.db`` (see
``agent.background_delegation``). Works from any process: ``stop`` sets a
flag the owning process polls, so it also works against a running gateway.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from janus_constants import get_janus_home

_LIVE = ("running",)

def _pid_alive(pid: int) -> bool:
    try:
        from gateway.status import _pid_exists
        return bool(_pid_exists(int(pid)))
    except Exception:
        return True


def _open_db():
    from janus_state import SessionDB
    return SessionDB(db_path=get_janus_home() / "state.db")


def _age(ts: Optional[float], now: float) -> str:
    if not ts:
        return "-"
    s = max(0, int(now - ts))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d"


def _resolve(db, ident: str) -> Optional[Dict[str, Any]]:
    """Find a delegation by full id or unique prefix."""
    ident = (ident or "").strip()
    if not ident:
        return None
    row = db.get_delegation(ident)
    if row:
        return row
    matches = [r for r in db.list_delegations(limit=1000) if r["id"].startswith(ident)]
    return matches[0] if len(matches) == 1 else None


def _reconcile(db) -> None:
    """Mark rows owned by dead processes orphaned (cheap, idempotent).

    Quiet on purpose (``announce=False``): this process cannot reach the
    parent session, so the row keeps ``delivered_at`` empty and the next
    gateway start-up (or in-process manager) still announces it once.
    """
    from agent.background_delegation import BackgroundDelegationManager

    mgr = BackgroundDelegationManager(db=db, sink=lambda evt: None, pid=0)
    try:
        mgr.reconcile_orphans(pid_alive=_pid_alive, announce=False)
    except Exception:
        pass


def _live_rows(db, limit: int) -> List[Dict[str, Any]]:
    """Running rows plus lost ones nobody has been told about yet."""
    rows = db.list_delegations(status=("running", "orphaned"), limit=limit)
    return [r for r in rows if r["status"] == "running" or r.get("delivered_at") is None]


def _cmd_list(db, args) -> int:
    _reconcile(db)
    show_all = bool(getattr(args, "all", False))
    limit = int(getattr(args, "limit", 50) or 50)
    rows = db.list_delegations(limit=limit) if show_all else _live_rows(db, limit)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0
    if not rows:
        print("\n  No background delegations" + ("." if show_all else " running.")
              + ("" if show_all else "  (--all shows finished ones)") + "\n")
        return 0
    now = time.time()
    print()
    for r in rows:
        goal = (r.get("goal") or "").replace("\n", " ")
        if len(goal) > 70:
            goal = goal[:67] + "..."
        when = _age(r.get("finished_at") or r.get("created_at"), now)
        tail = f"finished {when} ago" if r.get("finished_at") else f"started {when} ago"
        print(f"  {r['id']}  {r['status']:<11} {tail}")
        print(f"      goal:    {goal}")
        print(f"      session: {r.get('child_session_id') or '-'}"
              + (f"   parent: {r['parent_session_id']}" if r.get("parent_session_id") else ""))
        if r.get("error"):
            print(f"      error:   {str(r['error'])[:200]}")
        if r.get("summary"):
            first = str(r["summary"]).strip().splitlines()[0]
            print(f"      result:  {first[:160]}")
        if r.get("stop_requested") and r["status"] == "running":
            print("      stop requested — waiting for the owning process to interrupt it")
    print(f"\n  attach: janus delegations attach <id>    stop: janus delegations stop <id>\n")
    return 0


def _render_message(msg: Dict[str, Any]) -> List[str]:
    role = msg.get("role") or "?"
    lines: List[str] = []
    content = msg.get("content")
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    if role == "assistant":
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            name = fn.get("name") or "?"
            raw_args = fn.get("arguments") or ""
            preview = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
            lines.append(f"  🔧 {name} {preview[:160]}")
        if content:
            lines.append(f"  🤖 {content}")
    elif role == "tool":
        text = str(content or "").strip()
        if len(text) > 400:
            text = text[:400] + f"… [{len(text) - 400} more chars]"
        lines.append(f"  ↳ {msg.get('tool_name') or 'tool'}: {text}")
    elif role == "user":
        lines.append(f"  👤 {content}")
    elif content:
        lines.append(f"  [{role}] {content}")
    return lines


def _cmd_attach(db, args) -> int:
    row = _resolve(db, getattr(args, "delegation_id", None) or "")
    if not row:
        print("\n  No delegation matches that id (try `janus delegations list --all`).\n")
        return 1
    child = row.get("child_session_id")
    follow = bool(getattr(args, "follow", False))
    print(f"\n  delegation {row['id']}  [{row['status']}]  goal: {row.get('goal')}")
    print(f"  child session: {child or '-'}\n")
    if not child:
        print("  (no child session recorded)\n")
        return 0
    seen = 0
    while True:
        msgs = db.get_messages(child)
        for msg in msgs[seen:]:
            for line in _render_message(msg):
                print(line)
        seen = len(msgs)
        row = db.get_delegation(row["id"]) or row
        if not follow or row["status"] not in _LIVE:
            break
        time.sleep(1.0)
    print(f"\n  status: {row['status']}"
          + (f"\n  result: {row['summary']}" if row.get("summary") else "")
          + (f"\n  error:  {row['error']}" if row.get("error") else "") + "\n")
    return 0


def _cmd_stop(db, args) -> int:
    row = _resolve(db, getattr(args, "delegation_id", None) or "")
    if not row:
        print("\n  No delegation matches that id.\n")
        return 1
    if row["status"] not in _LIVE:
        print(f"\n  Delegation {row['id']} is not running (status: {row['status']}).\n")
        return 1
    db.request_delegation_stop(row["id"])
    print(f"\n  Stop requested for {row['id']}. The process running it will interrupt "
          f"the child within a few seconds; the partial transcript stays in session "
          f"{row.get('child_session_id') or '-'}.\n")
    return 0


def delegations_command(args) -> int:
    sub = getattr(args, "delegations_command", None) or "list"
    db = _open_db()
    try:
        if sub == "attach":
            return _cmd_attach(db, args)
        if sub == "stop":
            return _cmd_stop(db, args)
        return _cmd_list(db, args)
    finally:
        try:
            db.close()
        except Exception:
            pass
