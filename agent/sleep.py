"""Sleep — an offline memory-consolidation engine (the flagship).

Inspired by biological sleep and the 2026 "sleep-time compute" line of work:
while the agent is idle, a background cycle consolidates what it learned across
recent sessions, instead of carrying everything forward append-only. One cycle:

  1. REPLAY recent sessions + the daily journal.
  2. GRADUATE (multi-layer memory): distill recent sessions into semantic facts
     (memory_miner) and reusable procedures (skill_miner drafts) — the episodic
     journal -> semantic MEMORY.md -> procedural skills promotion the layers
     lacked.
  3. RECONCILE contradictions (memory_gardener).
  4. IMPORTANCE-TAG + FORGET: score entries by recency/reuse/substance and prune
     the lowest-salience ones (above a kept-floor) — logged to the journal, so
     history is never lost. This is the genuinely new bit; the gardener only
     handled contradictions and the curator only archived skills.
  5. SYNTHESIZE cross-session lessons spanning many sessions (not one).

Pure + injectable ``llm_caller`` so the cycle is testable without a model;
best-effort everywhere (a failure never breaks anything). It orchestrates the
existing learning modules — it does not reimplement them.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_IMPORTANCE_TARGET_LEN = 120  # chars; entries up to this score full on substance


# --- state ------------------------------------------------------------------

def get_sleep_state_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "sleep_state.json"


def sleep_log_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "sleep_log.jsonl"


def _now_iso() -> str:
    try:
        from janus_time import now as _now
        return _now().isoformat()
    except Exception:
        return ""


def _append_sleep_log(report: Dict[str, Any]) -> None:
    """Append one line summarizing a completed (non-dry-run) cycle. Guarded."""
    try:
        rec = {
            "ts": _now_iso(),
            "graduated_facts": int(report.get("graduated_facts", 0) or 0),
            "graduated_skills": int(report.get("graduated_skills", 0) or 0),
            "lessons": len(report.get("lessons") or []),
            "pruned": len(report.get("pruned") or []),
        }
        p = sleep_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # never break the cycle over a log line
        logger.debug("sleep-log append failed: %s", exc)


# --- DREAMS.md: the human-readable digest ----------------------------------

_DIGEST_HEADER = (
    "# Dreams\n\n"
    "What each sleep cycle consolidated, newest at the bottom. Written by Janus;\n"
    "safe to read, edit or delete. Turn off with `sleep.dreams_digest: false`.\n"
)
_DIGEST_ITEM_CHARS = 160   # one memory entry / lesson, collapsed to one line
_DIGEST_LIST_ITEMS = 15    # per section; the rest are counted, not listed


def dreams_path() -> Path:
    from tools.memory_tool import get_memory_dir
    return get_memory_dir() / "DREAMS.md"


def _one_line(text: Any) -> str:
    flat = " ".join(str(text).split())
    if len(flat) > _DIGEST_ITEM_CHARS:
        flat = flat[: _DIGEST_ITEM_CHARS - 1].rstrip() + "…"
    return flat


def _name_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("id") or item.get("path") or item)
    return str(item)


def _digest_list(title: str, items: List[Any]) -> List[str]:
    items = [i for i in items if str(i).strip()]
    if not items:
        return []
    lines = [f"- {title} ({len(items)}):"]
    lines += [f"  - {_one_line(i)}" for i in items[:_DIGEST_LIST_ITEMS]]
    if len(items) > _DIGEST_LIST_ITEMS:
        lines.append(f"  - …and {len(items) - _DIGEST_LIST_ITEMS} more")
    return lines


def render_dream_digest(report: Dict[str, Any], *, ts: str = "") -> str:
    """One markdown section describing what a cycle changed, in words."""
    when = (ts or "unknown time")[:16].replace("T", " ")
    lines: List[str] = []

    facts = int(report.get("graduated_facts", 0) or 0)
    skills = int(report.get("graduated_skills", 0) or 0)
    if facts or skills:
        lines.append(f"- Graduated {facts} fact(s) and {skills} skill draft(s) from recent sessions.")

    promote = report.get("promote")
    if isinstance(promote, dict):
        lines += _digest_list("Draft skills promoted", [_name_of(i) for i in promote.get("promoted") or []])
    lines += _digest_list("Retired as contradicted", list(report.get("reconciled") or []))
    lines += _digest_list("Forgotten as low-salience", list(report.get("pruned") or []))
    lines += _digest_list("Lessons synthesized across sessions", list(report.get("lessons") or []))

    playbook = report.get("playbook")
    if isinstance(playbook, dict) and (playbook.get("added") or playbook.get("revised")):
        lines.append(f"- Playbook: {int(playbook.get('added') or 0)} added, "
                     f"{int(playbook.get('revised') or 0)} revised.")

    challenge = report.get("self_challenge")
    if isinstance(challenge, dict) and challenge.get("attempted"):
        drafted = challenge.get("drafted") or []
        lines.append(f"- Practice: attempted {int(challenge.get('attempted') or 0)}, "
                     f"passed {int(challenge.get('passed') or 0)}, "
                     f"drafted {len(drafted)} quarantined skill(s).")

    lines += _digest_list("Skill improvements proposed (awaiting your review)",
                          [_name_of(i) for i in report.get("proposed") or []])
    lines += _digest_list("Proposals vetoed by the reviewer core",
                          [_name_of(i) for i in report.get("vetoed") or []])

    if not lines:
        lines = ["- Nothing to consolidate."]
    return f"## {when}\n\n" + "\n".join(lines) + "\n"


def _append_dream_digest(report: Dict[str, Any]) -> None:
    """Append this cycle's section to DREAMS.md. Guarded; never raises."""
    try:
        if not bool(_config("sleep", "dreams_digest", True)):
            return
        p = dreams_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fresh = not p.exists()
        with p.open("a", encoding="utf-8") as fh:
            if fresh:
                fh.write(_DIGEST_HEADER)
            fh.write("\n" + render_dream_digest(report, ts=_now_iso()))
    except Exception as exc:  # never break the cycle over the digest
        logger.debug("dreams digest append failed: %s", exc)


def read_dream_digest(cycles: int = 5) -> List[str]:
    """The digest sections, newest first; ``cycles`` <= 0 returns all."""
    try:
        text = dreams_path().read_text(encoding="utf-8")
    except OSError:
        return []
    sections: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                sections.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())
    sections.reverse()
    return sections if cycles <= 0 else sections[:cycles]


def load_sleep_state() -> Dict[str, Any]:
    path = get_sleep_state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def save_sleep_state(data: Dict[str, Any]) -> None:
    path = get_sleep_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- importance scoring -----------------------------------------------------

def _length_norm(text: str) -> float:
    return min(len(text) / _IMPORTANCE_TARGET_LEN, 1.0)


def importance_score(decay: float, reuse: float, length_norm: float) -> float:
    """Salience in [0,1]: recent (low decay) + reused + substantive scores high."""
    decay = max(0.0, min(1.0, decay))
    reuse = max(0.0, min(1.0, reuse))
    length_norm = max(0.0, min(1.0, length_norm))
    return round((1.0 - decay) * 0.5 + reuse * 0.3 + length_norm * 0.2, 4)


def score_memory_importance(
    entries: List[str], *, decays: Optional[Dict[str, float]] = None,
    reuses: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Importance per entry. ``decays``/``reuses`` (0..1) are optional signals;
    absent ones default to 0 (treated as recent / unreused)."""
    decays = decays or {}
    reuses = reuses or {}
    return {
        e: importance_score(decays.get(e, 0.0), reuses.get(e, 0.0), _length_norm(e))
        for e in entries
    }


def prune_low_salience(
    store: Any, *, threshold: float = 0.3, keep_min: int = 10,
    scores: Optional[Dict[str, float]] = None, apply: bool = True, target: str = "memory",
) -> List[str]:
    """Drop the lowest-salience entries below ``threshold``, never below the
    ``keep_min`` floor. Pruned entries are logged to the daily journal (history
    preserved). Returns the entries dropped (or that would be dropped)."""
    attr = "user_entries" if target == "user" else "memory_entries"
    entries = list(getattr(store, attr, []))
    if len(entries) <= keep_min:
        return []
    sc = scores if scores is not None else score_memory_importance(entries)
    below = sorted([e for e in entries if sc.get(e, 0.0) < threshold], key=lambda e: sc.get(e, 0.0))
    max_drop = len(entries) - keep_min
    to_drop = below[:max_drop]
    if apply:
        from tools.memory_tool import append_daily_snapshot
        for e in to_drop:
            try:
                store.remove(target, e)
                append_daily_snapshot(target, f"[sleep: pruned low-salience] {e}", "remove")
            except Exception as exc:
                logger.debug("prune remove failed: %s", exc)
    return to_drop


# --- LLM-backed steps -------------------------------------------------------

_SYNTH_SYSTEM = (
    "You consolidate several overlapping lessons — learned from repeated failures on the "
    "SAME kind of task — into ONE higher-level lesson naming the shared root cause and the "
    "corrective action. Be terse and concrete. Output a single sentence, no list."
)

_SYNTH_MIN_CLUSTER = 3  # only collapse a task_type with at least this many lessons


def _synthesize_one(
    task_type: str, items: List[str], *,
    llm_caller: Optional[Callable[..., Any]] = None,
    provider: Optional[str] = None, model: Optional[str] = None,
) -> str:
    """LLM-merge N overlapping lessons into one sentence. "" on failure."""
    try:
        joined = "\n".join(f"- {it}" for it in items if it.strip())
        if not joined.strip():
            return ""
        if llm_caller is None:
            from agent.auxiliary_client import call_llm as llm_caller
        prompt = (
            f"These {len(items)} lessons were learned from failures on '{task_type}' tasks "
            "and overlap. Distill them into ONE concise lesson capturing the shared root "
            "cause and corrective action. Return ONLY the single lesson sentence.\n\n"
            f"{joined[:8000]}"
        )
        response = llm_caller(
            task="sleep_synthesis", provider=provider, model=model,
            messages=[{"role": "system", "content": _SYNTH_SYSTEM},
                      {"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=300,
        )
        out = (response.choices[0].message.content or "").strip()
        return out.lstrip("-*• ").strip().strip('"').strip()
    except Exception as exc:
        logger.debug("lesson synthesis call failed: %s", exc)
        return ""


def _redteam_admits_synthesis(
    content: str, task_type: str, *, existing: str = "",
    llm_caller: Optional[Callable[..., Any]] = None,
) -> bool:
    """Gate a synthesized lesson through the dialectic red-team. Fails CLOSED:
    write only on an explicit ACCEPT; infra error or reject → False."""
    try:
        from agent.deliberation import apply_verdicts, red_team_claims
        claims = [{"id": "syn-0", "kind": "fact", "content": content,
                   "context": f"synthesized {task_type} lesson"}]
        rt = red_team_claims(claims, existing=existing, llm_caller=llm_caller)
        if rt.get("error") is not None:
            return False  # fail closed — do not write an unverified synthesis
        split = apply_verdicts(claims, rt.get("verdicts", {}))
        return len(split.get("accepted", [])) >= 1
    except Exception as exc:
        logger.debug("synthesis red-team failed: %s", exc)
        return False


def synthesize_cross_session_lessons(
    *, llm_caller: Optional[Callable[..., Any]] = None,
    provider: Optional[str] = None, model: Optional[str] = None,
    max_lessons: int = 5, min_cluster: int = _SYNTH_MIN_CLUSTER,
) -> List[Dict[str, Any]]:
    """Collapse near-duplicate failure lessons (same task_type) into one
    higher-level lesson each, gated by the dialectic red-team (fails closed).

    Reads the lessons store and writes each accepted synthesis via
    ``lessons.record_lesson(source="synthesis")``. Additive — the originals are
    kept; recency ranking (Increment 2.1) lets the broader synthesis surface
    first. Best-effort; returns the written records (``[]`` on failure / nothing
    to collapse).
    """
    written: List[Dict[str, Any]] = []
    try:
        from agent import lessons as _lessons
        groups: Dict[str, List[str]] = {}
        for r in _lessons.load():
            if str(r.get("source", "")) == "synthesis":
                continue  # never re-synthesize a synthesis
            tt = str(r.get("task_type", "general")) or "general"
            txt = str(r.get("lesson", "")).strip()
            if txt:
                groups.setdefault(tt, []).append(txt)
        for tt, items in groups.items():
            if len(items) < min_cluster:
                continue
            merged = _synthesize_one(tt, items, llm_caller=llm_caller, provider=provider, model=model)
            if not merged:
                continue
            if not _redteam_admits_synthesis(
                merged, tt, existing="\n".join(items), llm_caller=llm_caller
            ):
                continue
            rec = _lessons.record_lesson(merged, task_type=tt, source="synthesis")
            if rec:
                written.append(rec)
            if len(written) >= max_lessons:
                break
    except Exception as exc:
        logger.debug("cross-session synthesis failed: %s", exc)
    return written


# --- the cycle --------------------------------------------------------------

def _auto_evaluate_enabled() -> bool:
    """learning.self_improve.auto_evaluate — run fresh proposals through the eval
    suite during sleep. Off by default (real model cost); promotion still needs
    human approval regardless."""
    try:
        from janus_cli.config import load_config
        si = ((load_config().get("learning") or {}).get("self_improve") or {})
        return bool(si.get("auto_evaluate", False)) if isinstance(si, dict) else False
    except Exception:
        return False


def _twin_review_enabled() -> bool:
    """learning.self_improve.twin_review — red-team fresh proposals with a
    separate reviewer core that can only veto. Off by default."""
    try:
        from janus_cli.config import load_config
        si = ((load_config().get("learning") or {}).get("self_improve") or {})
        return bool(si.get("twin_review", False)) if isinstance(si, dict) else False
    except Exception:
        return False


def run_sleep_cycle(
    store: Any, *, llm_caller: Optional[Callable[..., Any]] = None,
    dry_run: bool = False, sessions: Optional[List[List[Dict[str, Any]]]] = None,
    session_summaries: Optional[List[str]] = None,
    prune_threshold: float = 0.3, keep_min: int = 10, prune_scores: Optional[Dict[str, float]] = None,
    provider: Optional[str] = None, model: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one consolidation cycle over ``store``. Returns a report. Never raises.

    ``dry_run`` reports what *would* happen (reconcile/prune in report-only mode)
    and skips additive graduation/synthesis. ``sessions`` (list of message lists)
    and ``session_summaries`` can be injected for tests; otherwise nothing is
    graduated (callers pass recent sessions).
    """
    report: Dict[str, Any] = {
        "dry_run": dry_run, "graduated_facts": 0, "graduated_skills": 0,
        "promote": None, "reconciled": [], "pruned": [], "lessons": [],
        "self_challenge": None, "proposed": [], "evaluated": [], "vetoed": [],
        "error": None,
    }
    try:
        # 2. GRADUATE — distill recent sessions into facts + skill drafts.
        if not dry_run and sessions:
            from agent.memory_miner import mine_session_memory
            from agent.skill_miner import mine_session_skills
            for msgs in sessions:
                try:
                    mm = mine_session_memory(msgs, store, llm_caller=llm_caller, provider=provider, model=model)
                    a = mm.get("added", {})
                    report["graduated_facts"] += a.get("memory", 0) + a.get("user", 0)
                except Exception as exc:
                    logger.debug("sleep graduate-memory failed: %s", exc)
                try:
                    ms = mine_session_skills(msgs, llm_caller=llm_caller, provider=provider, model=model)
                    report["graduated_skills"] += len(ms.get("written", []))
                except Exception as exc:
                    logger.debug("sleep graduate-skills failed: %s", exc)

        # 2b. PROMOTE — graduated-trust verifiable promotion of draft skills.
        # Opt-in (learning.governor.auto_promote) and gated by the governor's
        # admission state; keeps drafts drafted when the loop looks unhealthy.
        try:
            from agent.self_improvement_governor import auto_promote_enabled
            if auto_promote_enabled():
                from agent.skill_graph import auto_promote_drafts
                report["promote"] = auto_promote_drafts(llm_caller=llm_caller, dry_run=dry_run)
        except Exception as exc:
            logger.debug("sleep promote-drafts failed: %s", exc)

        # 3. RECONCILE contradictions.
        try:
            from agent.memory_gardener import reconcile
            rec = reconcile(store, llm_caller=llm_caller, apply=not dry_run, provider=provider, model=model)
            report["reconciled"] = rec.get("removed", [])
        except Exception as exc:
            logger.debug("sleep reconcile failed: %s", exc)

        # 4. IMPORTANCE-TAG + FORGET.
        report["pruned"] = prune_low_salience(
            store, threshold=prune_threshold, keep_min=keep_min,
            scores=prune_scores, apply=not dry_run)

        # 5. SYNTHESIZE: collapse near-duplicate failure lessons (same task_type)
        # into higher-level ones, dialectic-gated, written to the lessons store
        # (not memory). Cache-safe (offline). See plans/self-improvement-roadmap.md 2.2.
        if not dry_run:
            try:
                synth = synthesize_cross_session_lessons(
                    llm_caller=llm_caller, provider=provider, model=model)
                report["lessons"] = [r.get("lesson", "") for r in synth]
            except Exception as exc:
                logger.debug("sleep synthesize failed: %s", exc)

        # 6. ACE PLAYBOOK: improve the learning loop's OWN prompts. From this
        # cycle's activity, propose guidance, red-team it (fails closed), and
        # merge into the capped playbook. Off by default (learning.playbook).
        if not dry_run and session_summaries:
            try:
                from agent.playbook import enabled as _pb_enabled, run_curation
                if _pb_enabled():
                    report["playbook"] = run_curation(
                        "\n".join(str(s) for s in session_summaries), llm_caller=llm_caller)
            except Exception as exc:
                logger.debug("sleep playbook curation failed: %s", exc)

        # 7. SELF-CHALLENGE: generate verifiable practice tasks targeting known
        # weaknesses, attempt them in a sandboxed subagent, and draft a QUARANTINED
        # skill from each deterministically-verified success. Off by default
        # (self_challenge.enabled); best-effort — never breaks the cycle.
        if not dry_run:
            try:
                from agent.self_challenge import enabled as _sc_enabled, run_self_challenge
                if _sc_enabled():
                    report["self_challenge"] = run_self_challenge(
                        llm_caller=llm_caller, provider=provider, model=model)
            except Exception as exc:
                logger.debug("sleep self-challenge failed: %s", exc)

        # 8. PROPOSE: generate improved-skill-variant proposals for the worst
        # agent-created skills and record them for human review (Phase 2.1 — the
        # generator that feeds self_improve). Gated by learning.self_improve.enabled
        # + the governor + the autonomy floor; NEVER promotes (human approval
        # stays required). Off by default. Best-effort.
        if not dry_run:
            try:
                from agent.proposer import propose_skill_improvements
                proposed = propose_skill_improvements(llm_caller=llm_caller).get("proposed", [])
                report["proposed"] = proposed
                # 8b. EVALUATE (opt-in): run each fresh proposal through the eval
                # suite in an isolated profile so it carries promotable evidence.
                # Real model cost per proposal → gated by
                # learning.self_improve.auto_evaluate (default off); promotion
                # still needs human approval on top.
                if proposed and _auto_evaluate_enabled():
                    from agent.eval_orchestrator import evaluate_proposal
                    ev = []
                    for pid in proposed:
                        try:
                            if evaluate_proposal(pid).get("evaluated"):
                                ev.append(pid)
                        except Exception as exc:
                            logger.debug("sleep evaluate %s failed: %s", pid, exc)
                    report["evaluated"] = ev
                # 8c. TWIN-CORE REVIEW (opt-in): a separate reviewer core
                # red-teams each fresh proposal and may VETO it before a human
                # ever sees it — but can never approve one (mutual approval is
                # banned; human approval stays required). Gated by
                # learning.self_improve.twin_review (default off). Best-effort.
                if proposed and _twin_review_enabled():
                    from agent.twin_review import review_proposals
                    report["vetoed"] = review_proposals(
                        proposed, llm_caller=llm_caller).get("vetoed", [])
            except Exception as exc:
                logger.debug("sleep propose/evaluate failed: %s", exc)

        if not dry_run:
            state = load_sleep_state()
            state["last_run"] = _now_iso()
            state["last_report"] = {k: (len(v) if isinstance(v, list) else v)
                                    for k, v in report.items() if k != "error"}
            save_sleep_state(state)
            _append_sleep_log(report)
            _append_dream_digest(report)
    except Exception as exc:  # a consolidation failure must never break anything
        logger.debug("sleep cycle failed: %s", exc)
        report["error"] = str(exc)
    return report


# --- scheduling -------------------------------------------------------------

def _config(section: str, key: str, default: Any) -> Any:
    try:
        import yaml
        from janus_constants import get_janus_home
        cfg_path = get_janus_home() / "config.yaml"
        if cfg_path.is_file():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            sec = cfg.get(section, {})
            if isinstance(sec, dict) and key in sec:
                return sec[key]
    except Exception:
        pass
    return default


def _parse_iso(s: str) -> Optional[float]:
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def should_run_sleep(now_ts: float, state: Dict[str, Any], interval_hours: float) -> bool:
    """True if at least ``interval_hours`` have passed since the last run."""
    last = state.get("last_run")
    if not last:
        return True
    last_ts = _parse_iso(last)
    if last_ts is None:
        return True
    return (now_ts - last_ts) >= interval_hours * 3600


def _fetch_recent_sessions(
    limit: int = 10, *, since_ts: Optional[float] = None,
    sources: tuple = ("cli",),
) -> tuple:
    """Recent session transcripts + summaries for GRADUATE/SYNTHESIZE.

    Trust + dedup filters (the unattended path runs with no human eyeballing):
      - ``sources`` defaults to CLI-only, matching the trust assumption of the
        human-invoked ``janus sleep --now`` path. Gateway surfaces carry
        third-party content (Telegram/Discord/email strangers); mining them
        unattended would be a persistence vector for prompt injection. Widen
        via ``sleep.graduate_sources`` only deliberately.
      - child sessions (compression continuations, subagent scratch — rows
        with a ``parent_session_id``) are skipped: near-duplicates of their
        parents that would double aux-LLM mining spend and pollute memory.
      - ``since_ts`` (the previous cycle's ``last_run``) is the consolidation
        watermark: already-mined sessions are not re-fed every cycle, so a
        quiet instance costs nothing and skill drafts don't duplicate.
    Best-effort: ``([], [])`` when the session DB is unavailable.
    """
    sessions, summaries = [], []
    try:
        from janus_state import SessionDB
        from agent.memory_miner import _render_transcript
        db = SessionDB()
        try:
            for source in sources or ("cli",):
                if len(sessions) >= limit:
                    break
                for s in db.search_sessions(source=source, limit=limit * 3):
                    if len(sessions) >= limit:
                        break
                    if s.get("parent_session_id"):
                        continue
                    if since_ts is not None:
                        row_ts = _parse_iso(str(s.get("last_active")
                                                or s.get("started_at") or ""))
                        if row_ts is not None and row_ts <= since_ts:
                            continue
                    msgs = db.get_messages_as_conversation(s["id"])
                    if msgs:
                        sessions.append(msgs)
                        summaries.append(_render_transcript(msgs, max_chars=2000))
        finally:
            db.close()
    except Exception as exc:
        logger.debug("sleep session fetch failed: %s", exc)
    return sessions, summaries


def maybe_run_sleep(
    idle_for_seconds: Optional[float] = None, *, now_ts: Optional[float] = None,
    store: Any = None,
) -> Optional[Dict[str, Any]]:
    """Run a sleep cycle iff enabled, not paused, idle long enough, and due.
    Returns the report if it ran, else None. Best-effort."""
    try:
        if not bool(_config("sleep", "enabled", True)):
            return None
        state = load_sleep_state()
        if state.get("paused"):
            return None
        min_idle_hours = float(_config("sleep", "min_idle_hours", 2.0))
        if idle_for_seconds is not None and idle_for_seconds < min_idle_hours * 3600:
            return None
        if now_ts is None:
            import time
            now_ts = time.time()
        interval_hours = float(_config("sleep", "interval_hours", 168))
        if not should_run_sleep(now_ts, state, interval_hours):
            return None
        if store is None:
            from tools.memory_tool import MemoryStore
            store = MemoryStore()
            store.load_from_disk()
        # The unattended path used to call run_sleep_cycle WITHOUT sessions,
        # so GRADUATE/SYNTHESIZE only ever fired via `janus sleep --now`.
        # Feed it the same recent sessions the manual path uses — CLI-trusted
        # sources only, children excluded, watermarked to the previous cycle.
        _srcs = _config("sleep", "graduate_sources", ["cli"])
        _srcs = tuple(_srcs) if isinstance(_srcs, (list, tuple)) and _srcs else ("cli",)
        sessions, summaries = _fetch_recent_sessions(
            limit=int(_config("sleep", "recent_sessions", 10)),
            since_ts=_parse_iso(str(state.get("last_run") or "")),
            sources=_srcs,
        )
        return run_sleep_cycle(
            store,
            sessions=sessions or None,
            session_summaries=summaries or None,
            prune_threshold=float(_config("sleep", "importance_threshold", 0.3)),
            keep_min=int(_config("sleep", "keep_min_entries", 10)),
        )
    except Exception as exc:
        logger.debug("maybe_run_sleep failed: %s", exc)
        return None
