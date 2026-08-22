"""Eval orchestrator (Phase 2.1b) — turns a proposal into promotable evidence.

The proposer (agent/proposer.py) records skill-variant proposals but leaves them
without eval evidence, so ``self_improve.can_promote`` refuses them — a proposal
with no measured improvement can never pass the gate. This module is the missing
evaluate() seam the DGM-lite docstring anticipated: it runs the move-2 eval suite
twice —

  * BASELINE — the current skill, in an isolated throwaway copy of $JANUS_HOME.
  * VARIANT  — the same isolated copy with the proposed SKILL.md applied as the
    ACTIVE skill.

— each arm ``eval_trials`` times, and records the multi-trial means via
``self_improve.record_evaluation`` with an explicit noise-aware ``gate_ok``
(per-eval improvement floor + regression-flip veto + epsilon band — see
``_compute_gate``). The live profile is NEVER touched: the variant is applied only inside a temp home behind
a context-local ``JANUS_HOME`` override, which is torn down afterward. This is
the "evaluate the variant in an isolated profile, never the live one" isolation
the audit requires before a self-modification can be promoted.

Gated (self_improve.enabled + governor not frozen + autonomy floor clear) and
best-effort. Promotion still requires the human approval gate on top of the eval
evidence this produces.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def _skill_name_from_target(target: str) -> Optional[str]:
    """``skills/.drafts/<name>/SKILL.md`` → ``<name>``, else None."""
    from agent.skill_utils import draft_skill_name_from_target
    return draft_skill_name_from_target(target)


def _score(summary: Dict[str, Any]) -> Optional[float]:
    """The promotion score for a suite run: the mean SHAPED reward (Phase 2.3),
    which folds the secondary tool-failure penalty into the pass rate so a variant
    that passes cleanly outscores one that passes by thrashing. Falls back to the
    raw pass rate for older summaries with no shaped_score. None if no specs ran."""
    total = int(summary.get("total", 0) or 0)
    if total <= 0:
        return None
    shaped = summary.get("shaped_score")
    if isinstance(shaped, (int, float)):
        return round(float(shaped), 4)
    return round(int(summary.get("passed", 0) or 0) / total, 4)


def _resolve_variant_md(skills_dir: Path, name: str) -> Path:
    """Where to write a variant SKILL.md inside the isolated home.

    Skills live at ``skills/<category>/<name>/SKILL.md``; writing the variant
    to a flat ``skills/<name>/`` while the active copy survives elsewhere used
    to leave TWO SKILL.md files with the same frontmatter name — the skills
    index listed the skill twice and ``skill_view`` refused the ambiguous
    name, so the variant arm measured a *corrupted* install. Resolve the
    active skill's real path and overwrite in place; only a net-new skill
    (no active copy) gets a fresh flat directory.
    """
    from agent.skill_utils import resolve_active_skill_md
    existing = resolve_active_skill_md(skills_dir, name)
    if existing is not None:
        return existing
    sd = skills_dir / name
    sd.mkdir(parents=True, exist_ok=True)
    return sd / "SKILL.md"


def _run_isolated(live_home: Path, *, apply_variant: Optional[tuple],
                  agent_runner: Optional[Callable[..., Any]],
                  trials: int = 1) -> Optional[Dict[str, Any]]:
    """Copy the live home's skills + evals into a throwaway home, optionally
    overwrite one active skill with a variant body, run the eval suite there
    ``trials`` times behind a JANUS_HOME override. Returns an arm summary:

      {"score":  mean shaped score across trials,
       "per_eval_reward": {spec_name: mean shaped reward},
       "per_eval_pass":   {spec_name: pass fraction},
       "kinds":           {spec_name: "regression" | "capability"}}

    or None when no specs exist to measure against."""
    from janus_constants import (
        reset_janus_home_override, set_janus_home_override,
    )
    tmp = Path(tempfile.mkdtemp(prefix="janus-eval-"))
    token = None
    try:
        for sub in ("skills", "evals"):
            src = live_home / sub
            if src.is_dir():
                shutil.copytree(src, tmp / sub)
        if apply_variant is not None:
            name, content = apply_variant
            (_resolve_variant_md(tmp / "skills", name)).write_text(
                str(content), encoding="utf-8")
        token = set_janus_home_override(str(tmp))
        from agent.evals import load_eval_specs, run_evals
        specs = load_eval_specs()
        if not specs:
            return None
        scores: list = []
        rewards: Dict[str, list] = {}
        passes: Dict[str, list] = {}
        for _ in range(max(1, int(trials))):
            summary = run_evals(specs, agent_runner=agent_runner,
                                save_results=False)
            s = _score(summary)
            if s is None:
                return None
            scores.append(s)
            for r in summary.get("results", []):
                rname = str(r.get("name", ""))
                rewards.setdefault(rname, []).append(
                    float(r.get("reward", 1.0 if r.get("passed") else 0.0)))
                passes.setdefault(rname, []).append(bool(r.get("passed")))
        return {
            "score": round(sum(scores) / len(scores), 4),
            "per_eval_reward": {k: round(sum(v) / len(v), 4)
                                for k, v in rewards.items()},
            "per_eval_pass": {k: round(sum(1 for p in v if p) / len(v), 4)
                              for k, v in passes.items()},
            "kinds": {s.name: getattr(s, "kind", "regression") for s in specs},
        }
    finally:
        if token is not None:
            reset_janus_home_override(token)
        shutil.rmtree(tmp, ignore_errors=True)


# Gate thresholds. A regression-kind eval that was solid at baseline
# (pass fraction >= _FLIP_HIGH) and collapsed under the variant
# (<= _FLIP_LOW) is an unconditional veto. A variant must also show at
# least one *meaningful* per-eval gain (mean shaped reward + _MIN_GAIN)
# — so a no-op or fabricated equal pair can never pass — while the
# aggregate score may dip at most ``eval_epsilon`` below baseline.
_FLIP_HIGH = 0.8
_FLIP_LOW = 0.2
_MIN_GAIN = 0.5


def _compute_gate(before: Dict[str, Any], after: Dict[str, Any],
                  epsilon: float) -> tuple:
    """(gate_ok, detail) — the noise-aware promotion verdict for two arms."""
    flips = []
    for name, kind in (before.get("kinds") or {}).items():
        if str(kind) != "regression":
            continue
        b = float((before.get("per_eval_pass") or {}).get(name, 0.0))
        a = float((after.get("per_eval_pass") or {}).get(name, 0.0))
        if b >= _FLIP_HIGH and a <= _FLIP_LOW:
            flips.append(name)
    improvements = {
        name: round(float((after.get("per_eval_reward") or {}).get(name, 0.0))
                    - float(r), 4)
        for name, r in (before.get("per_eval_reward") or {}).items()
    }
    improved = [n for n, d in improvements.items() if d >= _MIN_GAIN]
    mean_ok = float(after.get("score", 0.0)) >= float(before.get("score", 0.0)) - float(epsilon)
    gate_ok = bool(not flips and improved and mean_ok)
    return gate_ok, {
        "regression_flips": flips,
        "improved_evals": improved,
        "mean_ok": mean_ok,
        "epsilon": float(epsilon),
        "per_eval_delta": improvements,
    }


def evaluate_proposal(
    proposal_id: str, *,
    agent_runner: Optional[Callable[..., Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate a skill-variant proposal against the eval suite and record the
    result. Returns a summary dict. Never raises.

    Gated: self_improve enabled + governor not frozen + autonomy floor clear.
    No-op (evaluated=False) when the proposal isn't an evaluable skill variant,
    or no eval specs exist to measure against."""
    out: Dict[str, Any] = {"evaluated": False, "reason": "",
                           "score_before": None, "score_after": None}
    try:
        from agent import self_improve
        if not self_improve.enabled(config):
            out["reason"] = "self_improve disabled"
            return out
        try:
            from agent.self_improvement_governor import learning_frozen
            if learning_frozen():
                out["reason"] = "governor frozen"
                return out
        except Exception:
            pass
        try:
            from agent.autonomy_guard import blocked_reason
            br = blocked_reason(config)
            if br:
                out["reason"] = f"autonomy floor: {br}"
                return out
        except Exception:
            pass

        rec = self_improve.get(proposal_id)
        if rec is None:
            out["reason"] = "no such proposal"
            return out
        if rec.get("kind") != "skill":
            out["reason"] = f"not an evaluable kind ({rec.get('kind')})"
            return out
        if rec.get("status") not in ("proposed", "evaluated"):
            out["reason"] = f"not in an evaluable state ({rec.get('status')})"
            return out
        name = _skill_name_from_target(rec.get("target", ""))
        if not name:
            out["reason"] = "could not resolve skill name from target"
            return out

        si = self_improve._cfg(config)
        trials = max(1, int(si.get("eval_trials", 2) or 1))
        epsilon = float(si.get("eval_epsilon", 0.05) or 0.0)

        from janus_constants import get_janus_home
        live = get_janus_home()
        before = _run_isolated(live, apply_variant=None,
                               agent_runner=agent_runner, trials=trials)
        if before is None:
            out["reason"] = "no eval specs to measure against"
            return out
        after = _run_isolated(live, apply_variant=(name, rec.get("content", "")),
                              agent_runner=agent_runner, trials=trials)
        if after is None:
            out["reason"] = "no eval specs to measure against"
            return out

        gate_ok, detail = _compute_gate(before, after, epsilon)
        detail["trials"] = trials
        self_improve.record_evaluation(
            proposal_id, score_before=before["score"], score_after=after["score"],
            gate_ok=gate_ok, detail=detail)
        out.update({"evaluated": True, "reason": "ok",
                    "score_before": before["score"], "score_after": after["score"],
                    "gate_ok": gate_ok, "detail": detail})
        try:
            from agent.audit_log import append_event
            append_event("self_improve_evaluated",
                         {"proposal_id": proposal_id, "skill": name,
                          "score_before": before["score"],
                          "score_after": after["score"],
                          "gate_ok": gate_ok})
        except Exception:
            pass
        return out
    except Exception as exc:
        logger.debug("evaluate_proposal failed: %s", exc)
        out["reason"] = f"error: {exc}"
        return out
