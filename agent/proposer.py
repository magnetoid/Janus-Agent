"""Autonomous self-improvement proposer (Phase 2.1) — the missing generator.

The DGM-lite engine in ``agent/self_improve.py`` had a complete gate/archive/
rollback layer but ZERO production callers: nothing proposed self-modifications,
so the "recursive" loop never actually turned. This module is that generator.

What it does, and — just as importantly — what it does NOT:

  * It detects persistently-failing AGENT-CREATED skills (never bundled/hub
    skills — same provenance invariant the curator honors) from the outcome
    tracker, asks the auxiliary model for an improved SKILL.md variant, and
    records it as a ``self_improve`` PROPOSAL targeting the skill drafts dir.
  * It NEVER evaluates and NEVER promotes. A proposal sits at status=proposed
    until a human reviews it with ``janus self-improve`` — promotion still
    requires the quadruple gate (measured eval improvement + governor OK +
    autonomy floor clear + human approval). Proposing is safe precisely because
    it only records a candidate; the write to a live artifact happens at promote.

Gated three ways so a default install proposes nothing: ``self_improve.enabled``
(off by default), the health governor (not FROZEN), and the autonomy safety
floor (no freeze / spend cap). Best-effort and offline — runs in the sleep
cycle, never touches the live conversation. Every proposal is recorded to the
tamper-evident audit log.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Only propose for a skill that has demonstrably underperformed over a real
# sample — not a one-off failure. Conservative defaults; caller-tunable.
_DEFAULT_MIN_USES = 4
_DEFAULT_MAX_SUCCESS_RATE = 0.5


def find_failing_skills(*, min_uses: int = _DEFAULT_MIN_USES,
                        max_success_rate: float = _DEFAULT_MAX_SUCCESS_RATE
                        ) -> List[Dict[str, Any]]:
    """Agent-created skills whose success rate is at/below ``max_success_rate``
    over at least ``min_uses`` recorded uses. Bundled/hub skills are excluded —
    the proposer only ever rewrites the agent's OWN artifacts."""
    out: List[Dict[str, Any]] = []
    try:
        from agent.outcome_tracker import skill_stats
        from tools.skill_usage import is_agent_created
        for name, s in skill_stats().items():
            rate = s.get("success_rate")
            if (int(s.get("uses", 0)) >= min_uses
                    and isinstance(rate, (int, float)) and rate <= max_success_rate):
                try:
                    if is_agent_created(name):
                        out.append({"name": name, **s})
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("find_failing_skills failed: %s", exc)
    # Worst first — spend the proposal budget where it helps most.
    out.sort(key=lambda f: (f.get("success_rate") or 0.0, -int(f.get("uses", 0))))
    return out


def _resolve_skill_md(name: str) -> Optional[Path]:
    """Path to an active skill's SKILL.md by its frontmatter name, or None."""
    try:
        from janus_constants import get_janus_home
        from agent.skill_utils import resolve_active_skill_md
        return resolve_active_skill_md(get_janus_home() / "skills", name)
    except Exception as exc:
        logger.debug("_resolve_skill_md failed: %s", exc)
        return None


_VARIANT_SYSTEM = (
    "You improve a Janus agent SKILL.md that has been FAILING in practice. "
    "You are given the current SKILL.md and lessons distilled from its failures. "
    "Return a COMPLETE revised SKILL.md — YAML frontmatter (keep the same "
    "`name`) then the body — that keeps the skill's purpose but fixes the failure "
    "mode: add missing preconditions, a checklist, or a guardrail. Output ONLY "
    "the SKILL.md text, no commentary, no code fences."
)


def _relevant_lessons(name: str, description: str) -> str:
    try:
        from agent.lessons import format_lessons_for_prompt, recall_lessons
        hits = recall_lessons(f"{name} {description}", n=5)
        return format_lessons_for_prompt(hits) if hits else ""
    except Exception:
        return ""


def _generate_variant(name: str, current_md: str, lessons: str,
                      llm_caller: Optional[Callable[..., Any]]) -> Optional[str]:
    """Ask the aux model for a revised SKILL.md. Returns valid SKILL.md text (a
    parseable frontmatter with the same name) or None."""
    try:
        if llm_caller is None:
            from agent.auxiliary_client import call_llm as llm_caller
        user = (f"CURRENT SKILL.md:\n{current_md}\n\n"
                f"FAILURE LESSONS:\n{lessons or '(none recorded)'}")
        resp = llm_caller(
            task="skills_hub",
            messages=[{"role": "system", "content": _VARIANT_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0, max_tokens=1500,
        )
        text = str(resp.choices[0].message.content or "").strip()
        # Strip a stray code fence if the model added one anyway.
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if not text:
            return None
        from agent.skill_utils import parse_frontmatter
        fm, body = parse_frontmatter(text)
        if not fm.get("name") or not body.strip():
            return None  # not a usable SKILL.md
        return text
    except Exception as exc:
        logger.debug("_generate_variant failed for %s: %s", name, exc)
        return None


def propose_skill_improvements(
    *, llm_caller: Optional[Callable[..., Any]] = None,
    max_proposals: int = 3, config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate improved-skill-variant PROPOSALS for the worst agent-created
    skills. Records each via ``self_improve.propose`` (status=proposed) for human
    review — never evaluates or promotes. Returns a summary dict. Never raises.

    Triple-gated: self_improve must be enabled, the governor must not be FROZEN,
    and the autonomy floor must be clear. Any gate closed → proposes nothing."""
    summary: Dict[str, Any] = {"proposed": [], "considered": 0, "reason": ""}
    try:
        from agent import self_improve
        if not self_improve.enabled(config):
            summary["reason"] = "self_improve disabled"
            return summary
        try:
            from agent.self_improvement_governor import learning_frozen
            if learning_frozen():
                summary["reason"] = "governor frozen"
                return summary
        except Exception:
            pass
        try:
            from agent.autonomy_guard import blocked_reason
            br = blocked_reason(config)
            if br:
                summary["reason"] = f"autonomy floor: {br}"
                return summary
        except Exception:
            pass

        failing = find_failing_skills()
        summary["considered"] = len(failing)
        for skill in failing[:max(0, int(max_proposals))]:
            name = skill["name"]
            md_path = _resolve_skill_md(name)
            if md_path is None:
                continue
            try:
                current = md_path.read_text(encoding="utf-8")
            except Exception:
                continue
            lessons = _relevant_lessons(name, skill.get("description", ""))
            variant = _generate_variant(name, current, lessons, llm_caller)
            if not variant:
                continue
            rate = skill.get("success_rate")
            rec = self_improve.propose(
                "skill", f"skills/.drafts/{name}/SKILL.md", variant,
                rationale=(f"'{name}' succeeded {rate} over {skill.get('uses')} uses; "
                           "proposed a revised variant addressing its failures."),
                config=config)
            if rec:
                summary["proposed"].append(rec["id"])
                try:
                    from agent.audit_log import append_event
                    append_event("self_improve_proposed",
                                 {"proposal_id": rec["id"], "kind": "skill",
                                  "skill": name, "success_rate": rate})
                except Exception:
                    pass
        if not summary["reason"]:
            summary["reason"] = "ok"
        return summary
    except Exception as exc:
        logger.debug("propose_skill_improvements failed: %s", exc)
        summary["reason"] = f"error: {exc}"
        return summary
