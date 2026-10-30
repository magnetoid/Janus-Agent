"""`janus usage --by …`: one grouping of the spend ledger at a time.

Invariant-style: every grouping must reconcile with the same totals, and a
turn the pricing layer could not value is counted as unpriced — shown as
"n/a", never folded into a $0 that looks free.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent import cost_ledger as cl

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def _row(days_ago, session, model, cost, *, status="actual", estimated=False, tokens=150):
    return {
        "ts": (NOW - timedelta(days=days_ago)).isoformat(),
        "session_id": session, "model": model,
        "input_tokens": tokens - 50, "output_tokens": 50, "cache_read_tokens": 0,
        "cost_usd": cost, "estimated": estimated, "status": status,
    }


ROWS = [
    _row(0.5, "s1", "opus", 0.50),
    _row(1.0, "s1", "opus", 0.25),
    _row(2.0, "s2", "haiku", 0.01),
    _row(3.0, "cron_ab12cd34ef56_20261025_090000", "opus", 0.30),
    _row(3.5, "cron_ab12cd34ef56_20261024_090000", "opus", 0.20),
    _row(4.0, "s3", "mystery", 0.02, status="estimated", estimated=True),
    # Legacy rows from before the `estimated` flag: $0 with an unknown status.
    _row(5.0, "s5", "deepseek-reasoner", 0.0, status="unknown"),
    _row(5.1, "s5", "deepseek-reasoner", 0.0, status=""),
    # Genuinely free: subscription-covered turns stay priced at $0.
    _row(6.0, "s6", "claude-sub", 0.0, status="included"),
    _row(45.0, "s9", "opus", 9.99),  # outside a 30-day window
]
META = {
    "s1": {"source": "cli", "title": "Refactor parser"},
    "s2": {"source": "telegram", "title": ""},
    "cron_ab12cd34ef56_20261025_090000": {"source": "cron", "title": "daily-blog · Oct 25"},
    "cron_ab12cd34ef56_20261024_090000": {"source": "cron", "title": "daily-blog · Oct 24"},
}
JOBS = {"ab12cd34ef56": "daily-blog"}


def _report(by, days=30, source=None):
    return cl.usage_by(by, days=days, rows=ROWS, now_ts=NOW_TS,
                       session_meta=META, job_names=JOBS, source=source)


@pytest.mark.parametrize("by", cl.USAGE_GROUPS)
def test_every_grouping_reconciles_with_the_totals(by):
    r = _report(by)
    groups = r["groups"]
    assert sum(g["turns"] for g in groups) == r["turns"]
    assert round(sum(g["priced_usd"] for g in groups), 6) == round(r["priced_usd"], 6)
    assert round(sum(g["estimated_usd"] for g in groups), 6) == round(r["estimated_usd"], 6)
    assert sum(g["unpriced_turns"] for g in groups) == r["unpriced_turns"]
    assert sum(g["input_tokens"] + g["output_tokens"] for g in groups) == r["tokens"]


def test_totals_are_the_same_whatever_the_grouping():
    totals = {by: (_report(by)["turns"], round(_report(by)["priced_usd"], 6)) for by in cl.USAGE_GROUPS}
    assert len(set(totals.values())) == 1


def test_window_excludes_old_rows():
    assert all(g["key"] != "s9" for g in _report("session")["groups"])
    assert _report("session", days=60)["turns"] == _report("session")["turns"] + 1


def test_unpriced_turns_are_never_counted_as_free():
    by_model = {g["key"]: g for g in _report("model")["groups"]}
    legacy = by_model["deepseek-reasoner"]
    assert legacy["unpriced_turns"] == legacy["turns"] == 2
    assert legacy["priced_usd"] == 0.0
    assert cl.format_usd(legacy) == "n/a"
    # Estimated rows carry a cost for the caps, but it is not a priced cost.
    mystery = by_model["mystery"]
    assert mystery["unpriced_turns"] == 1 and mystery["priced_usd"] == 0.0
    assert mystery["estimated_usd"] == pytest.approx(0.02)


def test_included_turns_are_priced_at_zero():
    sub = {g["key"]: g for g in _report("model")["groups"]}["claude-sub"]
    assert sub["unpriced_turns"] == 0
    assert cl.format_usd(sub) == "$0.00"


def test_partly_unpriced_group_is_marked():
    g = {"turns": 3, "unpriced_turns": 1, "priced_usd": 1.5}
    assert cl.format_usd(g).startswith("$1.50") and "n/a" in cl.format_usd(g)


def test_job_grouping_names_cron_jobs_and_pools_the_rest():
    jobs = {g["key"]: g for g in _report("job")["groups"]}
    assert jobs["ab12cd34ef56"]["label"] == "daily-blog"
    assert jobs["ab12cd34ef56"]["turns"] == 2 and jobs["ab12cd34ef56"]["sessions"] == 2
    assert jobs["(interactive)"]["turns"] == _report("job")["turns"] - 2


def test_session_grouping_uses_titles_when_known():
    sessions = {g["key"]: g for g in _report("session")["groups"]}
    assert sessions["s1"]["label"] == "Refactor parser"
    assert sessions["s5"]["label"] == "s5"


def test_platform_grouping_and_source_filter():
    platforms = {g["key"]: g for g in _report("platform")["groups"]}
    assert platforms["cron"]["turns"] == 2
    assert platforms["unknown"]["turns"] >= 1
    only_cron = _report("model", source="cron")
    assert only_cron["turns"] == 2


def test_groups_are_sorted_by_spend():
    groups = _report("model")["groups"]
    spend = [g["priced_usd"] + g["estimated_usd"] for g in groups]
    assert spend == sorted(spend, reverse=True)


def test_unknown_grouping_is_rejected():
    with pytest.raises(ValueError):
        cl.usage_by("color", rows=ROWS, now_ts=NOW_TS)
