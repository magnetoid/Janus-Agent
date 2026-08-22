"""Graduated-trust governor for the self-improvement loop.

The continual-learning metrics in ``outcome_tracker.learning_metrics()`` already
detect when the agent is degrading — forgetting, and a skill-diversity drop that
is the classic model-collapse early warning. Until now nothing *consumed* those
signals. This module is that missing consumer: a circuit-breaker that the
autonomous-admission paths (verifiable skill auto-promotion, and later mining
admission) check before changing the agent.

States:
  OK      — healthy; autonomous promotion runs at normal thresholds.
  CAUTION — mild decline; promotion runs but with tightened thresholds.
  FROZEN  — collapse / forgetting warning; autonomous promotion is paused.

Design contract (deliberately asymmetric):
  * **FROZEN on bad signals.** A health warning should pause self-modification —
    the safe direction is "don't change the agent while it's degrading."
  * **Fail-OPEN on exceptions.** A *crash* inside the governor must never block
    the agent; an internal error returns OK. (Uncertainty about health → pause;
    a broken governor → get out of the way.)

Everything here runs post-session / offline (sleep cron, CLI inspect) and reads
only ``outcomes.json`` — it never touches the live conversation or prompt cache.
Thresholds are imported from ``outcome_tracker`` so the warning logic and the
governor can never drift.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.outcome_tracker import (
    DIVERSITY_TREND_WARN,
    FORGETTING_WARN,
    FORWARD_TRANSFER_WARN,
    INSUFFICIENT_DATA_SESSIONS,
)

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_CAUTION = "caution"
STATE_FROZEN = "frozen"

_DEFAULTS = {
    "enabled": False,
    "auto_promote": False,
    "caution_ratio": 0.6,
    "caution_extra_uses": 2,
    "caution_success_floor": 0.85,
    "caution_reward_floor": 0.75,   # CAUTION raises graph.promotion_reward_threshold to ≥ this
}


def _gov_cfg(key: str, default: Any) -> Any:
    """Read a ``learning.governor.<key>`` value, best-effort."""
    try:
        from janus_cli.config import read_raw_config

        gov = (read_raw_config().get("learning") or {}).get("governor") or {}
        if isinstance(gov, dict) and key in gov and gov[key] is not None:
            return gov[key]
    except Exception:
        logger.debug("governor config read failed for %r", key, exc_info=True)
    return default


def governor_enabled() -> bool:
    return bool(_gov_cfg("enabled", _DEFAULTS["enabled"]))


def auto_promote_enabled() -> bool:
    """The write action (verifiable auto-promotion) is a separate opt-in."""
    return bool(_gov_cfg("auto_promote", _DEFAULTS["auto_promote"]))


def _eval_trend_freeze_reason() -> Optional[str]:
    """A failing regression gate → a strong 'getting worse' signal that
    warrants a freeze. Delegates to ``eval_trend.regression_gate`` — the same
    noise-floored, kind-aware signal CI fails on — so the governor also
    freezes on the reward-hacking signature (a suspicious one-point pass-rate
    jump), not just on declines. Best-effort; None when unavailable. Only the
    latest ``suite_hash`` is compared, so editing evals can't phantom-freeze.
    See plans/self-improvement-roadmap.md 3.2.
    """
    try:
        from agent.eval_trend import regression_gate

        gate = regression_gate()
        if not gate.get("ok", True):
            return f"eval gate failing: {gate.get('message')}"
    except Exception:
        logger.debug("governor eval-trend freeze read failed", exc_info=True)
    return None


def _eval_trend_caution_reason(min_drop: float = 0.05) -> Optional[str]:
    """A sustained downward eval pass-rate (without a net regression) → caution.
    Best-effort; None when unavailable / fewer than two points."""
    try:
        from agent.eval_trend import learning_curve

        points = (learning_curve().get("points") or [])
        if len(points) >= 2:
            first = float(points[0].get("pass_rate", 0.0))
            last = float(points[-1].get("pass_rate", 0.0))
            if last < first - min_drop:
                return f"eval pass-rate declining ({first:.2f} → {last:.2f})"
    except Exception:
        logger.debug("governor eval-trend caution read failed", exc_info=True)
    return None


def assess_admission_state(metrics: Optional[Dict[str, Any]] = None, *,
                           fail_closed: bool = False) -> Dict[str, Any]:
    """Classify the learning loop's health into OK / CAUTION / FROZEN.

    ``metrics`` is injectable for tests; otherwise it is pulled from
    ``outcome_tracker.learning_metrics()``. Never raises.

    ``fail_closed`` picks the direction on an INTERNAL ERROR (gap G7): an
    advisory read (``janus learning governor`` inspect) leaves it False and
    fails OPEN to OK — a crashed governor must not wedge the agent. A PROMOTION
    gate passes True and fails CLOSED to FROZEN — a crashed governor must block
    self-modification, not wave it through.
    """
    try:
        if not governor_enabled():
            return {"state": STATE_OK, "reasons": ["governor disabled"], "metrics": metrics or {}}

        if metrics is None:
            from agent.outcome_tracker import learning_metrics

            metrics = learning_metrics()
        metrics = metrics or {}

        # Eval-trend (deterministic benchmark) is an independent health signal,
        # applied regardless of the outcome-metric session count: a net regression
        # freezes; a milder sustained decline cautions. See roadmap 3.2.
        eval_freeze = _eval_trend_freeze_reason()
        if eval_freeze:
            return {"state": STATE_FROZEN, "reasons": [eval_freeze], "metrics": metrics}
        eval_caution = _eval_trend_caution_reason()

        sessions = metrics.get("sessions")
        if not isinstance(sessions, (int, float)) or sessions < INSUFFICIENT_DATA_SESSIONS:
            if eval_caution:
                return {"state": STATE_CAUTION, "reasons": [eval_caution], "metrics": metrics}
            return {
                "state": STATE_OK,
                "reasons": [f"insufficient data ({sessions} sessions)"],
                "metrics": metrics,
            }

        fgt = metrics.get("forgetting")
        dtrend = metrics.get("diversity_trend")
        fwt = metrics.get("forward_transfer")
        ratio = float(_gov_cfg("caution_ratio", _DEFAULTS["caution_ratio"]))

        reasons: List[str] = []

        # FROZEN: collapse / forgetting warnings — the same conditions that
        # populate learning_metrics()["warnings"].
        if isinstance(fgt, (int, float)) and fgt > FORGETTING_WARN:
            reasons.append(f"forgetting {fgt:.2f} > {FORGETTING_WARN} — skills regressing")
        if isinstance(dtrend, (int, float)) and dtrend < DIVERSITY_TREND_WARN:
            reasons.append(
                f"diversity trend {dtrend:.2f} < {DIVERSITY_TREND_WARN} — possible model collapse"
            )
        if reasons:
            return {"state": STATE_FROZEN, "reasons": reasons, "metrics": metrics}

        # CAUTION: milder decline, or a soft band approaching the freeze line.
        if isinstance(fwt, (int, float)) and fwt < FORWARD_TRANSFER_WARN:
            reasons.append(f"forward transfer {fwt:.2f} < {FORWARD_TRANSFER_WARN} — success declining")
        if isinstance(fgt, (int, float)) and fgt > FORGETTING_WARN * ratio:
            reasons.append(f"forgetting {fgt:.2f} approaching freeze threshold")
        if isinstance(dtrend, (int, float)) and dtrend < DIVERSITY_TREND_WARN * ratio:
            reasons.append(f"diversity trend {dtrend:.2f} approaching freeze threshold")
        if eval_caution:
            reasons.append(eval_caution)
        if reasons:
            return {"state": STATE_CAUTION, "reasons": reasons, "metrics": metrics}

        return {"state": STATE_OK, "reasons": ["learning loop healthy"], "metrics": metrics}
    except Exception:
        if fail_closed:
            logger.debug("governor assessment failed — failing CLOSED to FROZEN", exc_info=True)
            return {"state": STATE_FROZEN,
                    "reasons": ["governor error — failing closed (promotion gate)"],
                    "metrics": metrics or {}}
        logger.debug("governor assessment failed — failing open to OK", exc_info=True)
        return {"state": STATE_OK, "reasons": ["governor error — failing open"], "metrics": metrics or {}}


def admission_allowed(metrics: Optional[Dict[str, Any]] = None, *,
                      fail_closed: bool = False) -> bool:
    """True unless the governor is FROZEN. Autonomous admission gates on this.
    Promotion gates should pass ``fail_closed=True`` so a crashed governor
    blocks the promotion rather than allowing it (gap G7)."""
    return assess_admission_state(metrics, fail_closed=fail_closed).get("state") != STATE_FROZEN


def learning_frozen() -> bool:
    """True when the governor is enabled AND currently FROZEN — the signal the
    durable learning write-backs (memory/skill mining, model-strengths, reflexion)
    pause on, so the loop stops teaching future behavior while it looks unhealthy
    (gap G7). Default-off governor → always False (no behavior change unless the
    user turned the governor on). Fails to False (writes proceed) on any error —
    a crash in this signal must not itself halt learning."""
    try:
        return assess_admission_state().get("state") == STATE_FROZEN
    except Exception:
        logger.debug("learning_frozen check failed — allowing writes", exc_info=True)
        return False


def promotion_thresholds(metrics: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Threshold overrides for verifiable promotion, scaled to the current state.

    Returns ``None`` when FROZEN (caller must not promote). On OK returns an
    empty dict (use the ``graph.*`` config defaults). On CAUTION returns
    tightened ``min_uses`` / ``promo_thr`` / ``reward_thr`` the caller passes
    into ``skill_graph.assess_promotability``.
    """
    state = assess_admission_state(metrics).get("state")
    if state == STATE_FROZEN:
        return None
    if state != STATE_CAUTION:
        return {}

    try:
        from agent.skill_graph import _graph_cfg

        base_uses = int(_graph_cfg("min_uses_for_promotion", 3))
        base_thr = float(_graph_cfg("promotion_success_threshold", 0.75))
        base_reward = float(_graph_cfg("promotion_reward_threshold", 0.6))
    except Exception:
        base_uses, base_thr, base_reward = 3, 0.75, 0.6

    extra = int(_gov_cfg("caution_extra_uses", _DEFAULTS["caution_extra_uses"]))
    floor = float(_gov_cfg("caution_success_floor", _DEFAULTS["caution_success_floor"]))
    rfloor = float(_gov_cfg("caution_reward_floor", _DEFAULTS["caution_reward_floor"]))
    return {
        "min_uses": base_uses + extra,
        "promo_thr": max(base_thr, floor),
        # Every promotion bar rises under CAUTION, including the shaped-reward
        # floor — a bar the governor could not tighten would sit at its relaxed
        # default exactly when the loop looks shaky.
        "reward_thr": max(base_reward, rfloor),
    }
