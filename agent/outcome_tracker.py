"""Outcome-based reinforcement — learn from what WORKED, not just what was used.

The keystone of the learning loop: it records a success/failure signal per
session and attributes it to the skills used that session. With that reward
signal the system can promote skills/knowledge that correlate with success and
demote what correlates with failure — turning accumulation into getting smarter.

Store: ``~/.janus/learning/outcomes.json`` — append-only session records plus
derived per-skill aggregates computed on read.

Pure helpers with an injectable ``llm_caller`` so the classifier is testable
without a live model.
"""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_outcomes_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "outcomes.json"


def _now_iso() -> str:
    try:
        from janus_time import now as _now
        return _now().isoformat()
    except Exception:
        return ""


def load() -> List[Dict[str, Any]]:
    path = get_outcomes_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _save(records: List[Dict[str, Any]]) -> None:
    from agent.store_lock import atomic_write_text
    atomic_write_text(get_outcomes_path(),
                      json.dumps(records, indent=2, ensure_ascii=False) + "\n")


# A SUCCESS reached only after many failed tool calls is a weaker positive
# signal than a clean run — apply it as a SECONDARY penalty on the reward, never
# flipping the primary success verdict. See plans/self-improvement-roadmap.md 3.1.
_TOOL_FAILURE_PENALTY = 0.5


def record_outcome(
    session_id: str, success: bool, *, skills: Optional[List[str]] = None,
    note: str = "", active_persona: Optional[str] = None,
    tool_failure_rate: Optional[float] = None, friction: Optional[float] = None,
) -> Dict[str, Any]:
    """Record a session's outcome attributed to the skills it used.

    ``active_persona`` (when given) attributes the outcome to the persona that
    was active — the signal the persona optimizer learns from.

    ``tool_failure_rate`` (0..1, optional) applies a secondary penalty to the
    stored ``reward``: a session that only succeeded after many failed tool
    calls scores lower than a clean one.

    ``friction`` (0..1, optional; move 10) is the implicit-intervention density
    (interrupts / steers). It is RECORDED for observability but deliberately does
    NOT move the reward — its sign is unvalidated (interrupts often mean "add
    scope", not "you failed") and it is confounded across the session's skills,
    so feeding it to skill promotion would demote unrelated skills and teach the
    agent to be less steerable. When None it is read from the per-session sensor
    log. Aggregated into ``learning_metrics`` for a human to watch instead.
    """
    tfr = max(0.0, min(1.0, float(tool_failure_rate))) if tool_failure_rate is not None else 0.0
    if friction is None:
        try:
            from agent.feedback_signals import session_friction
            friction = session_friction(session_id)
        except Exception:
            friction = 0.0
    fric = max(0.0, min(1.0, float(friction or 0.0)))
    reward = round(max(0.0, (1.0 if success else 0.0) - _TOOL_FAILURE_PENALTY * tfr), 4)
    rec = {
        "session_id": session_id or "",
        "success": bool(success),
        "skills": sorted({s for s in (skills or []) if s}),
        "note": note,
        "persona": active_persona or "",
        "tool_failure_rate": tfr,
        "friction": fric,   # observability only — NOT folded into reward (see docstring)
        "reward": reward,
        "ts": _now_iso(),
    }
    from agent.store_lock import locked_store
    with locked_store(get_outcomes_path()):
        records = load()
        records.append(rec)
        _save(records)
    # Close the move-4 efficacy loop: credit/debit the lessons this session
    # recalled based on whether it succeeded. Best-effort; idempotent per session.
    try:
        from agent.lessons import reconcile_lesson_outcomes
        reconcile_lesson_outcomes(session_id, bool(success))
    except Exception:
        pass
    # Move 10: the implicit-feedback signals for this session have now been
    # folded into the reward — drop them so they can't double-count a later run.
    try:
        from agent.feedback_signals import clear_session
        clear_session(session_id)
    except Exception:
        pass
    return rec


def skill_success_trajectory(skill_name: str, window: int = 20) -> List[bool]:
    """Recent success/failure outcomes (oldest→newest) for sessions that used ``skill_name``.

    The per-skill reward trajectory the skill graph uses for verifiable promotion.
    """
    out = [bool(r.get("success")) for r in load() if skill_name in r.get("skills", [])]
    return out[-window:]


def skill_reward_trajectory(skill_name: str, window: int = 20) -> List[float]:
    """Recent penalised REWARDS (oldest→newest) for sessions that used ``skill_name``.

    Like ``skill_success_trajectory`` but continuous: a clean success scores 1.0,
    a tool-failure-riddled success scores lower (see ``record_outcome``). Records
    written before the reward field default to their boolean success.
    """
    out = [
        float(r["reward"]) if "reward" in r else (1.0 if r.get("success") else 0.0)
        for r in load() if skill_name in r.get("skills", [])
    ]
    return out[-window:]


def tool_failure_rate(snapshot: List[Dict[str, Any]]) -> float:
    """Fraction of tool results in ``snapshot`` that look like errors (cheap heuristic).

    Tool handlers signal failure via ``tool_error()`` → a JSON object beginning
    ``{"error": ...}``. Returns 0.0 when the snapshot has no tool results.
    """
    total = failed = 0
    for m in snapshot or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        total += 1
        c = str(m.get("content", "")).lstrip()
        cl = c.lower()
        if c.startswith('{"error"') or cl.startswith("error:") or '"error":' in cl[:200]:
            failed += 1
    return round(failed / total, 4) if total else 0.0


# ':' admits qualified names — plugin skills ("plugin:skill") and the
# shadow-trial alias ("draft:<name>"), whose drafts must accrue their OWN
# usage trajectory for the promotion gate.
_SKILL_VIEW = re.compile(r"skill_view\s*\(\s*name\s*=\s*['\"]([\w./:-]+)['\"]", re.I)


def skills_used_in(messages: List[Dict[str, Any]]) -> List[str]:
    """Best-effort: which skills were loaded this session (from skill_view calls)."""
    used: set = set()
    for m in messages:
        # tool_calls carry the structured call; content may carry the rendered text
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            if fn.get("name") == "skill_view":
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if args.get("name"):
                        used.add(str(args["name"]))
                except (ValueError, TypeError):
                    pass
        content = m.get("content")
        if isinstance(content, str):
            for hit in _SKILL_VIEW.findall(content):
                used.add(hit)
    return sorted(used)


def skill_stats() -> Dict[str, Dict[str, Any]]:
    """Per-skill aggregates: uses, successes, success_rate."""
    agg: Dict[str, Dict[str, Any]] = {}
    for rec in load():
        for skill in rec.get("skills", []):
            a = agg.setdefault(skill, {"uses": 0, "successes": 0})
            a["uses"] += 1
            if rec.get("success"):
                a["successes"] += 1
    for a in agg.values():
        a["success_rate"] = round(a["successes"] / a["uses"], 3) if a["uses"] else None
    return agg


def skill_success_rate(skill: str) -> Optional[float]:
    return skill_stats().get(skill, {}).get("success_rate")


def overall_stats() -> Dict[str, Any]:
    records = load()
    n = len(records)
    wins = sum(1 for r in records if r.get("success"))
    return {
        "sessions": n,
        "successes": wins,
        "success_rate": round(wins / n, 3) if n else None,
    }


def recent_success_rate(window: int = 20) -> Optional[float]:
    """Success rate over the most recent ``window`` sessions (trend signal)."""
    records = load()[-window:]
    if not records:
        return None
    wins = sum(1 for r in records if r.get("success"))
    return round(wins / len(records), 3)


# --- continual-learning instrumentation -------------------------------------
# Heuristic, session-level adaptations of the LifelongAgentBench metrics
# (Forward/Backward Transfer, Forgetting) plus a solution-diversity monitor.
# They answer "is the learning loop actually making the agent better — or
# quietly collapsing?" from the same append-only outcomes.json, no extra cost.
# These are PROXIES: they sharpen as task descriptions and skills recur; with a
# handful of one-off sessions they correctly return None ("insufficient data").

# Warning thresholds — the single source of truth. learning_metrics() turns
# these into the human-readable "warnings" list, and the self-improvement
# governor (agent/self_improvement_governor.py) imports the SAME constants so
# the two can never drift. Don't re-hardcode these numbers elsewhere.
FORGETTING_WARN = 0.2          # forgetting above this = some skills regressing
DIVERSITY_TREND_WARN = -0.15   # diversity dropping faster = model-collapse early warning
FORWARD_TRANSFER_WARN = -0.1   # lifetime success declining
_FRICTION_WARN = 0.5           # mean intervention friction above this = worth a human look
INSUFFICIENT_DATA_SESSIONS = 6  # below this many sessions, metrics aren't honest yet


def _rate(records: List[Dict[str, Any]]) -> Optional[float]:
    if not records:
        return None
    return sum(1 for r in records if r.get("success")) / len(records)


def _skill_counts(records: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in records:
        for s in r.get("skills", []):
            counts[s] = counts.get(s, 0) + 1
    return counts


def _normalized_entropy(counts: List[int]) -> Optional[float]:
    """Shannon entropy of a distribution, normalized to [0,1] by log(distinct)."""
    total = sum(counts)
    if total <= 0:
        return None
    distinct = sum(1 for c in counts if c > 0)
    if distinct <= 1:
        return 0.0
    h = -sum((c / total) * math.log(c / total) for c in counts if c > 0)
    return round(h / math.log(distinct), 3)


def forward_transfer(min_sessions: int = 6) -> Optional[float]:
    """Does experience speed later tasks? Late-third success rate minus early-third.

    Positive ⇒ the agent is getting better over its lifetime; negative ⇒ regressing.
    """
    records = load()
    if len(records) < min_sessions:
        return None
    k = max(1, len(records) // 3)
    early, late = _rate(records[:k]), _rate(records[-k:])
    if early is None or late is None:
        return None
    return round(late - early, 3)


def _per_skill_rate(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for r in records:
        for s in r.get("skills", []):
            a = agg.setdefault(s, {"uses": 0, "successes": 0})
            a["uses"] += 1
            if r.get("success"):
                a["successes"] += 1
    for a in agg.values():
        a["rate"] = a["successes"] / a["uses"] if a["uses"] else 0.0
    return agg


def backward_transfer(min_combined_uses: int = 4) -> Optional[float]:
    """Does newer learning help OLD task types? Mean per-skill (late_rate − early_rate).

    Computed over skills used in both the first and second half of history.
    Positive ⇒ later sessions lifted skills the agent already had.
    """
    records = load()
    if len(records) < 4:
        return None
    mid = len(records) // 2
    early, late = _per_skill_rate(records[:mid]), _per_skill_rate(records[mid:])
    deltas = [
        late[s]["rate"] - early[s]["rate"]
        for s in set(early) & set(late)
        if early[s]["uses"] + late[s]["uses"] >= min_combined_uses
    ]
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 3)


def forgetting(min_uses: int = 4, window: int = 10) -> Optional[float]:
    """Are skills regressing? Mean per-skill drop from earlier rate to recent rate.

    For each skill with enough history, max(0, earlier_rate − recent_rate);
    higher ⇒ more catastrophic forgetting / skill decay.
    """
    records = load()
    by_skill: Dict[str, List[bool]] = {}
    for r in records:
        for s in r.get("skills", []):
            by_skill.setdefault(s, []).append(bool(r.get("success")))
    drops: List[float] = []
    for seq in by_skill.values():
        if len(seq) < min_uses:
            continue
        # cap the recent window to half the history so an earlier baseline
        # always remains (a large window must not swallow the whole sequence)
        w = max(1, min(window, len(seq) // 2))
        recent = seq[-w:]
        earlier = seq[: len(seq) - w]
        if not earlier:
            continue
        er = sum(earlier) / len(earlier)
        rr = sum(recent) / len(recent)
        drops.append(max(0.0, er - rr))
    if not drops:
        return None
    return round(sum(drops) / len(drops), 3)


def skill_diversity(window: int = 20) -> Optional[float]:
    """Normalized entropy of skill usage over the recent window (0=collapsed, 1=even)."""
    counts = _skill_counts(load()[-window:])
    return _normalized_entropy(list(counts.values()))


def diversity_trend(window: int = 20) -> Optional[float]:
    """Recent skill-diversity minus the prior window's. Negative ⇒ narrowing.

    A leading indicator of self-training collapse (RAGEN): diversity/entropy
    drops *before* success rate does, so this is the early-warning signal.
    """
    records = load()
    if len(records) < window + 2:
        return None
    recent = _normalized_entropy(list(_skill_counts(records[-window:]).values()))
    older = _normalized_entropy(list(_skill_counts(records[-2 * window:-window]).values()))
    if recent is None or older is None:
        return None
    return round(recent - older, 3)


def learning_metrics(window: int = 20) -> Dict[str, Any]:
    """Bundle the continual-learning health signals + warnings + a summary line.

    Best-effort; every field is None when there's not enough data to be honest.
    """
    fwt = forward_transfer()
    bwt = backward_transfer()
    fgt = forgetting()
    div = skill_diversity(window)
    dtrend = diversity_trend(window)
    friction = mean_session_friction(window)
    overall = overall_stats()

    warnings: List[str] = []
    if fgt is not None and fgt > FORGETTING_WARN:
        warnings.append("forgetting detected — some skills are regressing")
    if dtrend is not None and dtrend < DIVERSITY_TREND_WARN:
        warnings.append("skill diversity dropping — possible over-narrowing / model collapse")
    if fwt is not None and fwt < FORWARD_TRANSFER_WARN:
        warnings.append("success rate declining over the agent's lifetime")
    if friction is not None and friction > _FRICTION_WARN:
        warnings.append(f"high interaction friction ({friction:.0%}) — users are "
                        "intervening often; worth reviewing recent sessions")

    if overall["sessions"] < INSUFFICIENT_DATA_SESSIONS:
        summary = f"Insufficient data ({overall['sessions']} sessions) — metrics sharpen with use."
    elif warnings:
        summary = "; ".join(warnings)
    else:
        trend = "improving" if (fwt or 0) > 0.05 else "stable"
        summary = f"Learning loop healthy and {trend} over {overall['sessions']} sessions."

    return {
        "sessions": overall["sessions"],
        "overall_success_rate": overall["success_rate"],
        "recent_success_rate": recent_success_rate(window),
        "forward_transfer": fwt,
        "backward_transfer": bwt,
        "forgetting": fgt,
        "skill_diversity": div,
        "diversity_trend": dtrend,
        "mean_friction": friction,
        "warnings": warnings,
        "summary": summary,
    }


def mean_session_friction(window: int = 20) -> Optional[float]:
    """Mean implicit-intervention friction over the last ``window`` sessions
    (move 10 observability). None when no session recorded a friction field."""
    vals = [float(r["friction"]) for r in load()[-window:]
            if isinstance(r.get("friction"), (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


# --- classification ---------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You judge whether an AI assistant SUCCEEDED at what the user asked in a "
    "conversation. Answer strictly with one word: SUCCESS, FAILURE, or UNCLEAR."
)


def classify_session(
    messages: List[Dict[str, Any]], *,
    llm_caller: Optional[Callable[..., Any]] = None,
    provider: Optional[str] = None, model: Optional[str] = None,
) -> Optional[bool]:
    """Judge whether the session succeeded. Returns True/False, or None if unclear.

    Best-effort; never raises. Injectable ``llm_caller`` for tests.
    """
    try:
        # Render a compact transcript (reuse the memory miner's flattener).
        from agent.memory_miner import _render_transcript
        transcript = _render_transcript(messages, max_chars=8000)
        if not transcript.strip():
            return None

        # Optional quorum mode (learning.dialectic.* — off by default): a
        # charitable judge and a strict judge label the session
        # independently; only agreement produces a label. Disagreement is
        # honestly "unclear" (None) — the existing contract — rather than
        # one judge's coin flip feeding skill success trajectories.
        from agent.deliberation import dialectic_enabled, quorum_classify
        if dialectic_enabled("outcomes"):
            quorum = quorum_classify(
                "Did the assistant succeed?", f"Conversation:\n{transcript}",
                llm_caller=llm_caller,
            )
            if quorum.get("error") is None:
                return quorum["label"]
            # infrastructure error → fall through to the single judge

        if llm_caller is None:
            from agent.auxiliary_client import call_llm as llm_caller
        response = llm_caller(
            task="outcome_classify",
            provider=provider, model=model,
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": f"Conversation:\n{transcript}\n\nDid the assistant succeed?"},
            ],
            temperature=0, max_tokens=5,
        )
        verdict = str(response.choices[0].message.content or "").strip().upper()
        if verdict.startswith("SUCCESS"):
            return True
        if verdict.startswith("FAILURE"):
            return False
        return None
    except Exception as exc:
        logger.debug("session outcome classification failed: %s", exc)
        return None
