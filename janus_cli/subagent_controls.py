"""``/agents`` subagent listing and stop/steer controls.

Shared by the classic CLI and the TUI backend. The live registry is
``tools.delegate_tool`` (``list_active_subagents`` / ``interrupt_subagent``
/ ``steer_child``); this module only formats rows and parses the
``/agents stop <id>`` and ``/agents steer <id> <message>`` subcommands.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable, Optional

USAGE = "Usage: /agents · /agents stop <id> · /agents steer <id> <message>"

_GOAL_CHARS = 72


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def subagent_rows(records: Iterable[dict[str, Any]], *, now: Optional[float] = None) -> list[str]:
    """One line per running subagent, indented by depth, in spawn order."""
    from tools.process_registry import format_uptime_short

    now = time.time() if now is None else now
    ordered = sorted(records, key=lambda r: (float(r.get("started_at") or 0), int(r.get("depth") or 0)))
    rows = []
    for record in ordered:
        depth = max(0, int(record.get("depth") or 0))
        tools = int(record.get("tool_count") or 0)
        elapsed = format_uptime_short(int(now - float(record.get("started_at") or now)))
        parts = [
            str(record.get("subagent_id") or "?"),
            elapsed,
            f"{tools} tool{'' if tools == 1 else 's'}",
            str(record.get("status") or "running"),
            _short(record.get("goal") or "", _GOAL_CHARS),
        ]
        rows.append("  " * depth + " · ".join(p for p in parts if p))
    return rows


def run_agents_action(args: str) -> Optional[str]:
    """Run ``stop``/``steer`` from ``/agents <args>``; None means "just list"."""
    from tools.delegate_tool import interrupt_subagent, steer_child

    parts = (args or "").split(None, 2)
    if not parts:
        return None
    action = parts[0].lower()
    if action == "stop" and len(parts) >= 2:
        sid = parts[1]
        if interrupt_subagent(sid):
            return f"Stopping subagent {sid} at its next step."
        return f"No running subagent with id {sid}."
    if action == "steer" and len(parts) == 3:
        sid, text = parts[1], parts[2]
        result = json.loads(steer_child(sid, text))
        if result.get("success"):
            return f"Sent to subagent {sid}; it sees the note after its current tool call."
        error = str(result.get("error") or "")
        if "No running child" in error:
            return f"No running subagent with id {sid}."
        return f"Could not steer {sid}: {error}"
    return USAGE
