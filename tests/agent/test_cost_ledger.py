"""Tests for the per-turn cost ledger + session spend ceiling (agent/cost_ledger.py)."""
from agent import cost_ledger as cl


# --------------------------------------------------------------------------
# Unpriced-model estimate (gap G9): a $0 turn WITH tokens must not be invisible
# to the spend cap.
# --------------------------------------------------------------------------

def test_zero_cost_with_tokens_gets_conservative_estimate():
    cl.record_turn("s", "mystery-model", input_tokens=1000, output_tokens=1000, cost_usd=0.0)
    row = cl.load_ledger()[0]
    assert row["cost_usd"] > 0.0        # not invisible to the cap
    assert row["estimated"] is True     # flagged so audits can tell it apart
    # accrues toward the session total the cap reads
    assert cl.session_total_usd("s") > 0.0


def test_real_cost_is_not_overwritten_or_flagged():
    cl.record_turn("s", "opus", input_tokens=1000, output_tokens=1000, cost_usd=0.05)
    row = cl.load_ledger()[0]
    assert row["cost_usd"] == 0.05
    assert row["estimated"] is False


def test_zero_cost_zero_tokens_stays_zero():
    # No usage → nothing to estimate; stays a true $0 row.
    cl.record_turn("s", "m", cost_usd=0.0)
    row = cl.load_ledger()[0]
    assert row["cost_usd"] == 0.0
    assert row["estimated"] is False


def test_included_subscription_turn_stays_zero():
    """A subscription-covered turn ($0, status='included') is genuinely free —
    it must NOT get a phantom estimate that could trip a subscription user's cap."""
    cl.record_turn("s", "gpt-5-codex", input_tokens=5000, output_tokens=5000,
                   cost_usd=0.0, status="included")
    row = cl.load_ledger()[0]
    assert row["cost_usd"] == 0.0
    assert row["estimated"] is False


def test_actual_measured_zero_stays_zero():
    cl.record_turn("s", "local", input_tokens=5000, output_tokens=5000,
                   cost_usd=0.0, status="actual")
    row = cl.load_ledger()[0]
    assert row["cost_usd"] == 0.0
    assert row["estimated"] is False


def test_unknown_status_still_estimates():
    cl.record_turn("s", "mystery", input_tokens=1000, output_tokens=1000,
                   cost_usd=0.0, status="unknown")
    row = cl.load_ledger()[0]
    assert row["cost_usd"] > 0.0
    assert row["estimated"] is True


def test_estimate_respects_config_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "janus_cli.config.load_config",
        lambda: {"budget": {"unpriced_usd_per_1k_tokens": 1.0}})
    cl.record_turn("s", "m", input_tokens=500, output_tokens=500, cost_usd=0.0)
    row = cl.load_ledger()[0]
    assert row["cost_usd"] == 1.0   # 1000 tokens * $1.0/1k
    assert row["estimated"] is True


def test_record_load_and_session_total():
    assert cl.record_turn("s1", "opus", input_tokens=100, output_tokens=20, cost_usd=0.01) is True
    assert cl.record_turn("s1", "opus", input_tokens=50, output_tokens=10, cost_usd=0.02) is True
    assert cl.record_turn("s2", "sonnet", cost_usd=0.005) is True
    rows = cl.load_ledger()
    assert len(rows) == 3
    assert rows[0]["session_id"] == "s1" and rows[0]["model"] == "opus"
    assert cl.session_total_usd("s1") == 0.03
    assert cl.session_total_usd("s2") == 0.005
    assert cl.session_total_usd("nope") == 0.0


def test_session_cost_limit_from_config():
    assert cl.session_cost_limit_usd({"budget": {"session_cost_usd": 1.5}}) == 1.5
    assert cl.session_cost_limit_usd({"budget": {"session_cost_usd": 0}}) is None    # 0 = unlimited
    assert cl.session_cost_limit_usd({}) is None                                     # default unlimited
    assert cl.session_cost_limit_usd({"budget": {"session_cost_usd": None}}) is None


def test_over_session_budget_logic():
    assert cl.over_session_budget(0.5, None) is False        # unlimited
    assert cl.over_session_budget(0.5, 0) is False           # 0 = unlimited
    assert cl.over_session_budget(0.4, 1.0) is False         # under ceiling
    assert cl.over_session_budget(1.0, 1.0) is True          # at ceiling
    assert cl.over_session_budget(1.2, 1.0) is True          # over ceiling


def test_record_turn_best_effort_on_bad_input():
    assert cl.record_turn("s", "m", cost_usd=None) is True   # None cost coerced to 0.0
    assert cl.load_ledger()[-1]["cost_usd"] == 0.0


# --- rolling spend caps (move 3: safety floor) ------------------------------

def _at(iso, cost):
    return {"ts": iso, "session_id": "s", "model": "m", "cost_usd": cost}


def test_spend_since_sums_window_across_sessions():
    now = 1_000_000.0
    from datetime import datetime, timezone
    def iso(offset):  # `offset` seconds before `now`
        return datetime.fromtimestamp(now - offset, tz=timezone.utc).isoformat()
    rows = [_at(iso(10), 1.0), _at(iso(100), 2.0), _at(iso(100000), 5.0)]
    # window covering the last 1000s catches the first two rows only
    assert cl.spend_since(now - 1000, rows=rows) == 3.0
    # unparseable/missing ts is EXCLUDED — counting it would inflate every
    # window monotonically and permanently trip the cap (no recovery path).
    assert cl.spend_since(now - 1000, rows=[{"ts": "not-a-date", "cost_usd": 4.0}]) == 0.0


def test_rolling_spend_day_and_month():
    import time
    from datetime import datetime, timezone
    now = time.time()
    def iso(offset):
        return datetime.fromtimestamp(now - offset, tz=timezone.utc).isoformat()
    for cost, off in [(1.0, 3600), (2.0, 3 * 86400), (8.0, 40 * 86400)]:
        cl.record_turn("s", "m", cost_usd=cost, ts=iso(off))
    assert cl.rolling_spend("day", now_ts=now) == 1.0        # only the 1h-ago row
    assert cl.rolling_spend("month", now_ts=now) == 3.0      # 1h + 3d rows


def test_spend_cap_exceeded_reason():
    cl.record_turn("s", "m", cost_usd=5.0)  # ts defaults to now
    reason = cl.spend_cap_exceeded({"budget": {"daily_usd": 3.0}})
    assert reason and "24h" in reason and "cap" in reason
    assert cl.spend_cap_exceeded({"budget": {"daily_usd": 100.0}}) is None  # under cap
    assert cl.spend_cap_exceeded({"budget": {}}) is None                    # unset


def test_cache_efficiency_hit_rate_and_switch_detection():
    # 3 turns: 80k cached / 20k fresh → 80% hit; session s2 switches models.
    cl.record_turn("s1", "model-a", input_tokens=10_000, cache_read_tokens=40_000,
                   cost_usd=0.01, status="actual")
    cl.record_turn("s2", "model-a", input_tokens=5_000, cache_read_tokens=40_000,
                   cost_usd=0.01, status="actual")
    cl.record_turn("s2", "model-b", input_tokens=5_000, cache_read_tokens=0,
                   cost_usd=0.01, status="actual")
    ce = cl.cache_efficiency()
    assert ce["turns"] == 3
    assert ce["cache_read_tokens"] == 80_000
    assert ce["fresh_input_tokens"] == 20_000
    assert abs(ce["hit_rate"] - 0.8) < 1e-9
    assert ce["sessions_with_model_switch"] == 1     # only s2 alternated


def test_cache_efficiency_empty_ledger_is_none():
    ce = cl.cache_efficiency()
    assert ce["hit_rate"] is None
    assert ce["turns"] == 0
