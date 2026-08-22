"""Invariants for the graduated-trust self-improvement governor.

Assert state-transition *relationships* on injected metrics (not snapshots),
the fail-open/freeze asymmetry, and that the freeze thresholds stay pinned to
the shared constants in outcome_tracker (so the two can't drift).
"""

from __future__ import annotations

import pytest

from agent import outcome_tracker as ot
from agent import self_improvement_governor as gov
from agent import skill_graph as sg


def _enable(monkeypatch, **cfg):
    """Turn the governor on and make config reads return their defaults."""
    monkeypatch.setattr(gov, "governor_enabled", lambda: True)
    monkeypatch.setattr(gov, "_gov_cfg", lambda key, default: cfg.get(key, default))


def _metrics(**kw):
    base = {
        "sessions": 10, "forgetting": 0.0,
        "diversity_trend": 0.0, "forward_transfer": 0.05, "summary": "ok",
    }
    base.update(kw)
    return base


# ── freeze conditions ────────────────────────────────────────────────────


def test_frozen_on_forgetting(monkeypatch):
    _enable(monkeypatch)
    r = gov.assess_admission_state(_metrics(forgetting=0.25))
    assert r["state"] == gov.STATE_FROZEN
    assert any("forgetting" in reason for reason in r["reasons"])


def test_frozen_on_diversity_collapse(monkeypatch):
    _enable(monkeypatch)
    r = gov.assess_admission_state(_metrics(diversity_trend=-0.2))
    assert r["state"] == gov.STATE_FROZEN
    assert any("collapse" in reason or "diversity" in reason for reason in r["reasons"])


# ── milder / healthy ─────────────────────────────────────────────────────


def test_caution_on_declining_transfer(monkeypatch):
    _enable(monkeypatch)
    r = gov.assess_admission_state(_metrics(forward_transfer=-0.15))
    assert r["state"] == gov.STATE_CAUTION


def test_ok_when_healthy(monkeypatch):
    _enable(monkeypatch)
    assert gov.assess_admission_state(_metrics())["state"] == gov.STATE_OK


def test_insufficient_data_is_ok_even_with_bad_signal(monkeypatch):
    _enable(monkeypatch)
    # Below the honesty floor, metrics aren't trusted — must not freeze.
    r = gov.assess_admission_state(_metrics(sessions=3, forgetting=0.9))
    assert r["state"] == gov.STATE_OK


# ── gating ───────────────────────────────────────────────────────────────


def test_disabled_governor_is_ok(monkeypatch):
    monkeypatch.setattr(gov, "governor_enabled", lambda: False)
    assert gov.assess_admission_state(_metrics(forgetting=0.9))["state"] == gov.STATE_OK


def test_admission_allowed_reflects_state(monkeypatch):
    monkeypatch.setattr(gov, "assess_admission_state",
                        lambda metrics=None, **kw: {"state": gov.STATE_FROZEN})
    assert gov.admission_allowed() is False
    monkeypatch.setattr(gov, "assess_admission_state",
                        lambda metrics=None, **kw: {"state": gov.STATE_CAUTION})
    assert gov.admission_allowed() is True


# ── promotion thresholds scale with state ────────────────────────────────


def test_promotion_thresholds_frozen_is_none(monkeypatch):
    _enable(monkeypatch)
    assert gov.promotion_thresholds(_metrics(forgetting=0.25)) is None


def test_promotion_thresholds_ok_is_empty(monkeypatch):
    _enable(monkeypatch)
    assert gov.promotion_thresholds(_metrics()) == {}


def test_promotion_thresholds_caution_tightens(monkeypatch):
    _enable(monkeypatch)
    thr = gov.promotion_thresholds(_metrics(forward_transfer=-0.15))
    assert thr is not None and thr != {}
    # Defaults: graph min_uses 3 (+2), success 0.75 → floor 0.85.
    assert thr["min_uses"] >= 5
    assert thr["promo_thr"] >= 0.85


def test_promotion_thresholds_caution_tightens_reward_floor_too(monkeypatch):
    """CAUTION must raise EVERY promotion bar, not just the ones that predate it.

    assess_promotability gates on the shaped mean reward as well as the boolean
    success rate (gap G10). A bar the governor cannot tighten would sit at its
    relaxed default exactly when the learning loop looks shaky — the opposite of
    what CAUTION means.
    """
    _enable(monkeypatch)
    thr = gov.promotion_thresholds(_metrics(forward_transfer=-0.15))
    assert thr is not None
    assert thr["reward_thr"] >= 0.75, "reward floor must rise under CAUTION"
    assert thr["reward_thr"] > float(sg._graph_cfg("promotion_reward_threshold", 0.6))


# ── fail-open on exceptions (vs freeze-on-bad-signal) ────────────────────


def test_fail_open_on_garbage_metrics(monkeypatch):
    _enable(monkeypatch)
    assert gov.assess_admission_state({"sessions": "garbage"})["state"] == gov.STATE_OK


def test_fail_open_when_metrics_source_raises(monkeypatch):
    _enable(monkeypatch)

    def boom():
        raise RuntimeError("metrics unavailable")

    monkeypatch.setattr(ot, "learning_metrics", boom)
    # metrics=None → pulls learning_metrics() → raises → must fail OPEN, not crash.
    assert gov.assess_admission_state()["state"] == gov.STATE_OK


# ── the freeze boundary is the shared constant, not a private copy ───────


def test_freeze_boundary_pinned_to_shared_constant(monkeypatch):
    _enable(monkeypatch)
    just_over = gov.assess_admission_state(_metrics(forgetting=ot.FORGETTING_WARN + 0.01))
    just_under = gov.assess_admission_state(
        _metrics(forgetting=ot.FORGETTING_WARN - 0.01, forward_transfer=0.05))
    assert just_over["state"] == gov.STATE_FROZEN
    assert just_under["state"] != gov.STATE_FROZEN


# ── eval-trend as a second freeze signal (Increment 3.2) ─────────────────────

def test_eval_regression_freezes_even_with_healthy_metrics(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(gov, "_eval_trend_freeze_reason",
                        lambda: "eval suite regressed: 3 eval(s) pass→fail vs 0 learned")
    r = gov.assess_admission_state(_metrics())  # outcome metrics are healthy
    assert r["state"] == gov.STATE_FROZEN
    assert "regressed" in r["reasons"][0]


def test_eval_decline_cautions(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(gov, "_eval_trend_caution_reason",
                        lambda *a, **k: "eval pass-rate declining (0.80 → 0.60)")
    r = gov.assess_admission_state(_metrics())
    assert r["state"] == gov.STATE_CAUTION
    assert any("declining" in x for x in r["reasons"])


def test_eval_decline_cautions_even_with_insufficient_sessions(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(gov, "_eval_trend_caution_reason",
                        lambda *a, **k: "eval pass-rate declining (0.9 → 0.7)")
    r = gov.assess_admission_state(_metrics(sessions=2))  # outcome data insufficient
    assert r["state"] == gov.STATE_CAUTION


def test_eval_trend_freeze_reason_delegates_to_gate(monkeypatch):
    # The governor freezes exactly when the regression gate fails: any
    # consistent regression-kind flip (noise-floored) or a suspicious jump.
    # It no longer nets regressions against learned evals — with the noise
    # floor, a consistent pass→fail flip is a real signal on its own.
    monkeypatch.setattr("agent.eval_trend.regression_gate",
                        lambda *a, **k: {"ok": False, "message": "REGRESSION — 1 eval(s)"})
    reason = gov._eval_trend_freeze_reason()
    assert reason is not None and "REGRESSION" in reason
    monkeypatch.setattr("agent.eval_trend.regression_gate",
                        lambda *a, **k: {"ok": True, "message": "OK"})
    assert gov._eval_trend_freeze_reason() is None
    # A jump is advisory (gate stays ok=True), so it must NOT freeze the loop —
    # a genuine win should never lock out self-improvement.
    monkeypatch.setattr("agent.eval_trend.regression_gate",
                        lambda *a, **k: {"ok": True, "message": "OK — no regressions  [!] pass rate rose +40% ..."})
    assert gov._eval_trend_freeze_reason() is None
