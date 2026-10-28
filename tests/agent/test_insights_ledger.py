"""`janus insights` surfaces the per-turn spend ledger (plan item 2.4).

The sessions table gives *estimated* cost from token counts; the ledger
(`agent/cost_ledger.py`) is the per-turn record the rolling spend caps enforce
and the only place that knows which turns were unpriced. Insights must show
both so the user can reconcile them.
"""
import pytest

from agent import cost_ledger as cl
from agent.insights import InsightsEngine
from janus_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "insights.db")
    yield session_db
    session_db.close()


def _seed(db):
    db.create_session(session_id="s1", source="cli", model="opus")
    db.create_session(session_id="s2", source="telegram", model="haiku")
    db.update_token_counts("s1", input_tokens=1000, output_tokens=100)
    db.update_token_counts("s2", input_tokens=1000, output_tokens=100)
    cl.record_turn("s1", "opus", input_tokens=1000, output_tokens=100, cost_usd=0.40)
    cl.record_turn("s2", "haiku", input_tokens=1000, output_tokens=100, cost_usd=0.01)
    cl.record_turn("s2", "mystery", input_tokens=500, output_tokens=50, cost_usd=0.0)  # unpriced


def test_report_carries_ledger_summary_keyed_by_session_source(db):
    _seed(db)
    report = InsightsEngine(db).generate(days=30)
    ledger = report["ledger"]
    assert ledger["days"] == 30
    assert ledger["sessions"] == 2
    by_source = {p["source"]: p for p in ledger["by_source"]}
    assert by_source["cli"]["cost_usd"] == 0.40
    assert "telegram" in by_source and "unknown" not in by_source
    assert ledger["unpriced_turns"] == 1


def test_source_filter_applies_to_the_ledger_too(db):
    _seed(db)
    report = InsightsEngine(db).generate(days=30, source="cli")
    assert report["ledger"]["total_usd"] == 0.40
    assert [p["source"] for p in report["ledger"]["by_source"]] == ["cli"]


def test_terminal_output_shows_ledger_total_and_unpriced_warning(db):
    _seed(db)
    engine = InsightsEngine(db)
    text = engine.format_terminal(engine.generate(days=30))
    assert "Spend ledger" in text
    assert "$0.4" in text                      # the recorded (not re-estimated) total
    assert "unpriced" in text.lower()          # the estimated turn is called out


def test_terminal_output_without_ledger_rows_says_so(db):
    db.create_session(session_id="s1", source="cli", model="opus")
    db.update_token_counts("s1", input_tokens=10, output_tokens=10)
    engine = InsightsEngine(db)
    text = engine.format_terminal(engine.generate(days=30))
    assert "Spend ledger" in text
    assert "no ledger rows" in text.lower()


def test_gateway_output_includes_ledger_total(db):
    _seed(db)
    engine = InsightsEngine(db)
    text = engine.format_gateway(engine.generate(days=30))
    assert "ledger" in text.lower()
    assert "$0.4" in text


def test_empty_sessions_report_still_has_ledger_key(db):
    report = InsightsEngine(db).generate(days=30)
    assert report["empty"] is True
    assert report["ledger"]["turns"] == 0


def test_ledger_spend_is_rendered_even_when_no_session_started_in_window(db):
    # A resumed session's turns land in the ledger while the session row itself
    # predates the window (or is missing) — the spend must not disappear behind
    # the "No sessions found" short-circuit.
    cl.record_turn("resumed-long-ago", "opus", input_tokens=1000, output_tokens=100, cost_usd=0.40)
    engine = InsightsEngine(db)
    report = engine.generate(days=30)
    assert report["empty"] is True
    terminal = engine.format_terminal(report)
    assert "No sessions found" in terminal
    assert "Spend ledger" in terminal and "$0.4" in terminal
    gateway = engine.format_gateway(report)
    assert "No sessions found" in gateway
    assert "$0.4" in gateway
