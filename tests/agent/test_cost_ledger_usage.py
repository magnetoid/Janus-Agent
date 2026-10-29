"""Tests for the ledger-backed usage summary (agent/cost_ledger.usage_summary).

The summary is what `janus insights` shows as "Spend ledger": the per-turn
cost rows the rolling spend caps actually enforce, aggregated over a trailing
window. Invariant-style — totals must reconcile across every breakdown, and
unpriced (estimated) turns must stay visible rather than collapsing to $0.
"""
from datetime import datetime, timedelta, timezone

from agent import cost_ledger as cl

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def _row(days_ago: float, session: str, model: str, cost: float, *,
         estimated: bool = False, in_tok: int = 100, out_tok: int = 50, ts=None):
    return {
        "ts": ts if ts is not None else (NOW - timedelta(days=days_ago)).isoformat(),
        "session_id": session,
        "model": model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_tokens": 0,
        "cost_usd": cost,
        "estimated": estimated,
        "status": "estimated" if estimated else "actual",
    }


ROWS = [
    _row(0.5, "s1", "opus", 0.50),
    _row(1.0, "s1", "opus", 0.25),
    _row(2.0, "s2", "haiku", 0.01),
    _row(3.0, "s3", "mystery", 0.02, estimated=True),
    _row(45.0, "s9", "opus", 9.99),          # outside a 30-day window
    _row(0.1, "s4", "opus", 1.00, ts=""),    # unparseable ts → excluded
]
SOURCES = {"s1": "cli", "s2": "telegram", "s3": "cron"}


def test_total_reconciles_across_every_breakdown():
    s = cl.usage_summary(days=30, rows=ROWS, now_ts=NOW_TS, session_sources=SOURCES)
    assert s["total_usd"] == round(0.50 + 0.25 + 0.01 + 0.02, 6)
    assert round(sum(m["cost_usd"] for m in s["by_model"]), 6) == s["total_usd"]
    assert round(sum(p["cost_usd"] for p in s["by_source"]), 6) == s["total_usd"]
    assert round(sum(d["cost_usd"] for d in s["by_day"]), 6) == s["total_usd"]
    assert s["turns"] == 4
    assert s["sessions"] == 3


def test_rows_outside_window_or_without_timestamp_are_excluded():
    s = cl.usage_summary(days=30, rows=ROWS, now_ts=NOW_TS)
    models = {m["model"] for m in s["by_model"]}
    assert "opus" in models
    assert s["total_usd"] < 9.0          # the 45-day-old row is out
    assert all(d["date"] >= "2026-09-28" for d in s["by_day"])
    # widen the window and the old row comes in; the ts-less row never does
    wide = cl.usage_summary(days=60, rows=ROWS, now_ts=NOW_TS)
    assert wide["total_usd"] == round(s["total_usd"] + 9.99, 6)


def test_estimated_turns_stay_visible_and_split_from_priced():
    s = cl.usage_summary(days=30, rows=ROWS, now_ts=NOW_TS)
    assert s["unpriced_turns"] == 1
    assert s["estimated_usd"] == 0.02
    assert round(s["priced_usd"] + s["estimated_usd"], 6) == s["total_usd"]
    mystery = next(m for m in s["by_model"] if m["model"] == "mystery")
    assert mystery["estimated_usd"] == 0.02
    assert mystery["unpriced_turns"] == 1


def test_source_breakdown_uses_session_mapping_with_unknown_fallback():
    s = cl.usage_summary(days=30, rows=ROWS, now_ts=NOW_TS,
                         session_sources={"s1": "cli"})
    by = {p["source"]: p for p in s["by_source"]}
    assert by["cli"]["cost_usd"] == 0.75
    assert by["cli"]["sessions"] == 1
    assert by["unknown"]["sessions"] == 2


def test_source_filter_restricts_to_matching_sessions():
    s = cl.usage_summary(days=30, rows=ROWS, now_ts=NOW_TS,
                         session_sources=SOURCES, source="telegram")
    assert s["total_usd"] == 0.01
    assert s["sessions"] == 1
    assert [p["source"] for p in s["by_source"]] == ["telegram"]


def test_breakdowns_are_sorted_by_cost_descending():
    s = cl.usage_summary(days=30, rows=ROWS, now_ts=NOW_TS, session_sources=SOURCES)
    costs = [m["cost_usd"] for m in s["by_model"]]
    assert costs == sorted(costs, reverse=True)
    days = [d["date"] for d in s["by_day"]]
    assert days == sorted(days)           # the daily series stays chronological


def test_empty_ledger_yields_zeroed_summary():
    s = cl.usage_summary(days=30, rows=[], now_ts=NOW_TS)
    assert s["turns"] == 0 and s["sessions"] == 0
    assert s["total_usd"] == 0.0
    assert s["by_model"] == [] and s["by_source"] == [] and s["by_day"] == []


def test_default_rows_come_from_the_ledger_file():
    cl.record_turn("live", "opus", input_tokens=10, output_tokens=10, cost_usd=0.30)
    s = cl.usage_summary(days=30)
    assert s["total_usd"] == 0.30
    assert s["sessions"] == 1
