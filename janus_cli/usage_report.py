"""`janus usage`: recorded spend from the cost ledger, grouped one way at a time.

The numbers come from ``<janus_home>/learning/cost_ledger.jsonl`` — the same
rows the rolling spend caps enforce — via :func:`agent.cost_ledger.usage_by`.
``janus insights --usage`` shows the session-table estimate next to it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from agent import cost_ledger

_GROUP_TITLES = {
    "model": "Model", "platform": "Platform", "session": "Session",
    "day": "Day", "job": "Cron job",
}


def _session_meta() -> Dict[str, Dict[str, str]]:
    try:
        from janus_state import SessionDB
        db = SessionDB()
        try:
            rows = db._conn.execute(
                "SELECT id, source, title, parent_session_id FROM sessions").fetchall()
        finally:
            db.close()
        return {r[0]: {"source": r[1] or "", "title": r[2] or "", "parent_session_id": r[3] or ""}
                for r in rows}
    except Exception:
        return {}


def _job_names() -> Dict[str, str]:
    try:
        from cron.jobs import load_jobs
        return {str(j.get("id")): str(j.get("name") or j.get("id")) for j in load_jobs() if j.get("id")}
    except Exception:
        return {}


def _tokens(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def render(report: Dict[str, Any], *, limit: Optional[int] = 25) -> str:
    title = _GROUP_TITLES.get(report["by"], report["by"])
    lines = [f"Spend over the last {report['days']} days, by {title.lower()}", ""]
    groups = report["groups"]
    if not groups:
        lines.append("  No recorded turns in this window.")
        return "\n".join(lines)
    shown = groups if limit is None else groups[:limit]
    width = min(max(len(title), *(len(g["label"]) for g in shown)), 48)
    lines.append(f"  {title:<{width}}  {'Turns':>6}  {'Tokens':>8}  {'Cost':>14}")
    for g in shown:
        label = g["label"] if len(g["label"]) <= width else g["label"][: width - 1] + "…"
        tokens = _tokens(g["input_tokens"] + g["output_tokens"] + g["cache_read_tokens"])
        lines.append(f"  {label:<{width}}  {g['turns']:>6}  {tokens:>8}  {cost_ledger.format_usd(g):>14}")
    if limit is not None and len(groups) > limit:
        lines.append(f"  … {len(groups) - limit} more (use --limit 0 for all)")
    lines.append("")
    total = {"turns": report["turns"], "unpriced_turns": report["unpriced_turns"],
             "priced_usd": report["priced_usd"]}
    lines.append(f"  Total: {report['turns']} turns in {report['sessions']} sessions, "
                 f"{_tokens(report['tokens'])} tokens, {cost_ledger.format_usd(total)}")
    if report["unpriced_turns"]:
        lines.append(f"  n/a: {report['unpriced_turns']} turns couldn't be priced "
                     f"(spend caps count them at an estimated ${report['estimated_usd']:,.2f}).")
    return "\n".join(lines)


def cmd_usage(args) -> int:
    report = cost_ledger.usage_by(
        args.by, days=args.days, session_meta=_session_meta(),
        job_names=_job_names() if args.by == "job" else None, source=args.source,
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render(report, limit=args.limit or None))
    return 0
