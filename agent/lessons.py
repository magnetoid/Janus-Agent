"""Reflexion write-back — turn failures into retrievable lessons.

Where ``outcome_tracker`` records *that* a session failed and ``memory_miner``
distills durable *facts*, this distills the actionable LESSON from a failure:
a short natural-language note ("next time, do X instead of Y") keyed to the
task type, so the agent can pull it back the next time a similar task comes up.

This is the Reflexion pattern (Shinn et al., 2023) applied across sessions:
verbalized self-critique stored in episodic memory and retrieved on demand.
It compounds — every failure makes the next similar attempt a little better —
and unlike memory facts, a lesson is explicitly tied to a *kind of task*.

Store: ``~/.janus/learning/lessons.json`` — append-only, capped to the most
recent ``_MAX_LESSONS`` so it can't grow without bound. Pure helpers with an
injectable ``llm_caller`` so reflection is testable without a live model; every
function is best-effort and never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_LESSONS = 300  # keep the most recent; oldest are dropped on write
_MAX_RECALL_SESSIONS = 1000  # cap the pending recall→outcome join state

_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "and", "or",
    "in", "on", "for", "with", "my", "your", "it", "its", "that", "this", "i",
    "you", "we", "they", "as", "at", "by", "from", "but", "not", "do", "does",
    "next", "time", "when", "use", "using", "instead",
}


def get_lessons_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "lessons.json"


def _now_iso() -> str:
    try:
        from janus_time import now as _now
        return _now().isoformat()
    except Exception:
        return ""


def _lesson_id(text: str) -> str:
    """Stable id for a lesson — a hash of its normalized text, so the same
    lesson always maps to the same id (aligns with dedup) and survives reloads."""
    return hashlib.sha256(str(text).strip().lower().encode("utf-8")).hexdigest()[:12]


def _migrate(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Fill move-4 fields (id + efficacy counters) on an older record. In-memory
    only — persisted the next time something writes the record back."""
    if isinstance(rec, dict):
        rec.setdefault("helpful", 0)
        rec.setdefault("harmful", 0)
        # Provenance (gap G3): whether this lesson was vetted by the dialectic
        # red-team gate before it entered the store. Absent on legacy records →
        # default False so a missing flag never reads as "vetted".
        rec.setdefault("screened", False)
        if not rec.get("id") and rec.get("lesson"):
            rec["id"] = _lesson_id(rec["lesson"])
    return rec


def load() -> List[Dict[str, Any]]:
    path = get_lessons_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [_migrate(r) for r in data]
    except (ValueError, OSError):
        return []


def _save(records: List[Dict[str, Any]]) -> None:
    from agent.store_lock import atomic_write_text
    atomic_write_text(get_lessons_path(),
                      json.dumps(records, indent=2, ensure_ascii=False) + "\n")


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2 and w not in _STOP}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return s or "general"


def record_lesson(
    lesson: str, *, task_type: str = "", session_id: str = "", source: str = "reflexion",
    screened: bool = False,
) -> Optional[Dict[str, Any]]:
    """Append a lesson, de-duplicating exact repeats. Returns the record (or None).

    ``screened`` records whether the lesson passed the dialectic red-team gate
    (gap G3). It defaults False: direct callers on trusted paths (curated writes,
    sleep SYNTHESIZE — already gated upstream) leave it unset; the auto-distilled
    paths (reflexion, compression) set it via ``screen_lesson`` so an unvetted
    lesson is never silently indistinguishable from a vetted one."""
    lesson = str(lesson).strip()
    if not lesson:
        return None
    from agent.store_lock import locked_store
    with locked_store(get_lessons_path()):
        records = load()
        norm = lesson.lower()
        if any(str(r.get("lesson", "")).lower() == norm for r in records):
            return None  # already learned this
        rec = {
            "id": _lesson_id(lesson),
            "lesson": lesson,
            "task_type": _slug(task_type) if task_type else "general",
            "session_id": session_id or "",
            "source": source,
            "screened": bool(screened),
            "helpful": 0,   # sessions that recalled this and then SUCCEEDED
            "harmful": 0,   # sessions that recalled this and then FAILED
            "ts": _now_iso(),
        }
        records.append(rec)
        if len(records) > _MAX_LESSONS:
            records = records[-_MAX_LESSONS:]
        _save(records)
    return rec


def _screen_enabled() -> bool:
    """Red-team screening of auto-distilled lessons — on by default whenever the
    learning loop runs. Opt out with ``learning.screen_lessons: false``."""
    try:
        from agent.feature_flags import flag_enabled
        return flag_enabled("learning", "screen_lessons", default=True)
    except Exception:
        return True


def screen_lesson(
    lesson: str, *, task_type: str = "", transcript: str = "", existing: str = "",
    llm_caller: Optional[Callable[..., Any]] = None,
) -> tuple[Optional[str], bool]:
    """Vet one auto-distilled lesson through the dialectic red-team gate before
    it can be persisted (gap G3 — poisoned lessons otherwise persist and re-enter
    every future turn via push-recall).

    Returns ``(text, screened)``:
      * accepted → (lesson-or-revised-text, True)
      * rejected → (None, True)
      * gate disabled / infra error → (lesson, False) — fail OPEN, an infra
        failure is not a rejection, but the caller marks the record unvetted.
    """
    lesson = str(lesson).strip()
    if not lesson:
        return None, True
    if not _screen_enabled():
        return lesson, False
    try:
        from agent.deliberation import apply_verdicts, red_team_claims
        claim = [{"id": "lesson", "kind": "fact", "content": lesson,
                  "context": task_type}]
        res = red_team_claims(claim, transcript=transcript, existing=existing,
                              llm_caller=llm_caller)
        if res.get("error"):
            return lesson, False  # infra failure → fail open, unscreened
        split = apply_verdicts(claim, res.get("verdicts", {}))
        if split["accepted"]:
            return str(split["accepted"][0]["content"]).strip(), True
        return None, True  # rejected by the arbiter
    except Exception as exc:
        logger.debug("screen_lesson failed: %s", exc)
        return lesson, False


def record_failure_lesson(
    messages: List[Dict[str, Any]], *, session_id: str = "",
    llm_caller: Optional[Callable[..., Any]] = None,
    provider: Optional[str] = None, model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Distill a failed session into a lesson AND vet it through the red-team
    gate before persisting (gap G3). This is the screened replacement for the
    raw ``reflect_on_failure`` → ``record_lesson`` path in auto_mine. Returns
    the stored record, or None if nothing was distilled or the gate rejected it.
    Best-effort; never raises."""
    try:
        from agent.memory_miner import _render_transcript
        r = reflect_on_failure(messages, llm_caller=llm_caller,
                               provider=provider, model=model)
        if not r or not r.get("lesson"):
            return None
        transcript = ""
        try:
            transcript = _render_transcript(messages, max_chars=8000)
        except Exception:
            pass
        text, screened = screen_lesson(
            r["lesson"], task_type=r.get("task_type", ""),
            transcript=transcript, llm_caller=llm_caller)
        if text is None:
            logger.debug("reflexion lesson rejected by red-team gate (session=%s)",
                         session_id or "-")
            return None
        return record_lesson(text, task_type=r.get("task_type", ""),
                             session_id=session_id, source="reflexion",
                             screened=screened)
    except Exception as exc:
        logger.debug("record_failure_lesson failed: %s", exc)
        return None


# --- efficacy: join recalled lessons to the outcome they rode with ----------

def get_recall_state_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "lesson_recall.json"


def _load_recall_state() -> Dict[str, List[str]]:
    p = get_recall_state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _save_recall_state(state: Dict[str, List[str]]) -> None:
    # Cap to the most recent sessions so an un-reconciled backlog can't grow
    # without bound (dicts preserve insertion order → drop the oldest).
    if len(state) > _MAX_RECALL_SESSIONS:
        state = dict(list(state.items())[-_MAX_RECALL_SESSIONS:])
    from agent.store_lock import atomic_write_text
    atomic_write_text(get_recall_state_path(), json.dumps(state, ensure_ascii=False))


def _outcome_tracking_on() -> bool:
    """True when learning.track_outcomes is set — the only path that reconciles
    the recall log. Gate logging on it so a default install (recall injection on,
    outcome tracking off) doesn't pay per-turn IO to accumulate state nothing reads."""
    try:
        from janus_cli.config import load_config
        return bool(((load_config().get("learning") or {}).get("track_outcomes")))
    except Exception:
        return False


def log_recall(session_id: str, lesson_ids: List[str]) -> None:
    """Record that ``lesson_ids`` were surfaced to ``session_id`` this turn.

    Serves two consumers: the efficacy loop (a later outcome credits/debits
    the ids — only consumed when outcome tracking is on) and per-session
    recall DEDUP in ``recall_context_for_turn`` (a lesson already in the
    conversation is never re-injected). Recorded unconditionally so dedup
    works even with tracking off. Best-effort; accumulates per session."""
    try:
        ids = [i for i in (lesson_ids or []) if i]
        if not session_id or not ids:
            return
        from agent.store_lock import locked_store
        with locked_store(get_recall_state_path()):
            state = _load_recall_state()
            merged = sorted(set(state.get(session_id, [])) | set(ids))
            # Re-insert at the end so an actively-recalling long session stays
            # fresh against the insertion-order LRU cap (dicts drop
            # oldest-inserted first).
            state.pop(session_id, None)
            state[session_id] = merged
            _save_recall_state(state)
    except Exception as exc:
        logger.debug("log_recall failed: %s", exc)


def reconcile_lesson_outcomes(session_id: str, success: bool) -> int:
    """Credit (success) or debit (failure) every lesson recalled in ``session_id``.

    Idempotent: the session's recall record is POPPED, so a second call is a
    no-op. Returns how many lessons were updated. Best-effort — this is the
    signal recall ranking and pruning use to demote lessons that don't help.
    """
    try:
        if not session_id:
            return 0
        from agent.store_lock import locked_store
        with locked_store(get_recall_state_path()):
            state = _load_recall_state()
            ids = state.pop(session_id, None)
            if ids:
                _save_recall_state(state)  # pop first → idempotent even if crediting fails
        if not ids:
            return 0
        field = "helpful" if success else "harmful"
        n = 0
        with locked_store(get_lessons_path()):
            records = load()
            by_id = {r.get("id"): r for r in records if r.get("id")}
            for lid in ids:
                r = by_id.get(lid)
                if r is not None:
                    r[field] = int(r.get(field, 0) or 0) + 1
                    n += 1
            if n:
                _save(records)
        return n
    except Exception as exc:
        logger.debug("reconcile_lesson_outcomes failed: %s", exc)
        return 0


def _efficacy_multiplier(rec: Dict[str, Any]) -> float:
    """Laplace-smoothed success rate → a gentle recall-score multiplier in
    [0.75, 1.25]. Unknown (no outcomes) is neutral (1.0); a lesson that keeps
    riding with failed sessions sinks toward 0.75, a helpful one rises toward
    1.25 — a soft nudge that never overwhelms lexical relevance.

    CAVEAT (why this is only a soft RANKING signal, never a delete criterion):
    the signal is confounded. A lesson is credited/debited for the WHOLE session
    outcome with no relevance/used check, so a lesson recalled on inherently-hard
    tasks tracks the task's base failure rate, not its own value. Demoting such a
    lesson in ranking is self-correcting (a genuinely useful one still surfaces
    when strongly relevant); DELETING it on this signal would remove correct
    guidance for exactly the hardest tasks — so we deliberately do not.
    """
    h = int(rec.get("helpful", 0) or 0)
    harm = int(rec.get("harmful", 0) or 0)
    return 0.75 + 0.5 * (h + 1) / (h + harm + 2)


# --- reflection (failure -> lesson) -----------------------------------------

_REFLECT_SYSTEM = (
    "You are doing a blameless post-mortem on an AI assistant's FAILED attempt at a "
    "task. Identify the single most useful, transferable lesson that would make the "
    "next similar attempt succeed. Be concrete and actionable — name what to do "
    "differently, not just what went wrong. Ignore one-off causes (a flaky network, "
    "a typo the user fixed). If there is no generalizable lesson, say so."
)


def _build_reflect_prompt(transcript: str) -> str:
    return (
        "From this failed session, extract ONE transferable lesson as JSON:\n"
        '  {"task_type": "<short kebab-case kind, e.g. \\"deploy-staging\\" or '
        '\\"sql-migration\\">",\n'
        '   "lesson": "<one or two sentences: what to do differently next time>"}\n'
        "Return {\"task_type\": \"\", \"lesson\": \"\"} if nothing generalizable applies. "
        "Return ONLY the JSON object.\n\n"
        f"SESSION:\n{transcript}"
    )


def _parse_reflection(raw: Optional[str]) -> Optional[Dict[str, str]]:
    if not raw or not raw.strip():
        return None
    m = re.search(r"\{.*\}", str(raw), re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    lesson = str(data.get("lesson", "")).strip()
    if not lesson:
        return None
    return {"task_type": str(data.get("task_type", "")).strip(), "lesson": lesson}


def reflect_on_failure(
    messages: List[Dict[str, Any]], *,
    llm_caller: Optional[Callable[..., Any]] = None,
    provider: Optional[str] = None, model: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Distill one transferable lesson from a failed session. None if none applies.

    Best-effort; never raises. Injectable ``llm_caller`` for tests.
    """
    try:
        from agent.memory_miner import _render_transcript
        transcript = _render_transcript(messages, max_chars=8000)
        if not transcript.strip():
            return None
        if llm_caller is None:
            from agent.auxiliary_client import call_llm as llm_caller
        # ACE playbook: prepend learned guidance for the lesson-distillation step
        # (no-op when the playbook is disabled/empty).
        from agent.playbook import augment_system
        system_prompt = augment_system(_REFLECT_SYSTEM, "lessons")
        response = llm_caller(
            task="reflexion_lesson",
            provider=provider, model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _build_reflect_prompt(transcript)},
            ],
            temperature=0, max_tokens=200,
        )
        return _parse_reflection(response.choices[0].message.content)
    except Exception as exc:
        logger.debug("failure reflection failed: %s", exc)
        return None


# --- recall (lesson retrieval) ----------------------------------------------

# Recency: a lesson's lexical score is multiplied by a half-life decay on its
# age, so a freshly-learned lesson outranks an equally-relevant stale one. The
# half-life (days) is configurable via learning.lessons.recency_half_life_days;
# 0 disables decay (pure lexical). See plans/self-improvement-roadmap.md 2.1.
_RECENCY_HALF_LIFE_DAYS = 30.0


def _recency_half_life_days(config: Optional[Dict[str, Any]] = None) -> float:
    """Read the recency half-life (days) from config; default 30, 0 disables."""
    try:
        if config is None:
            from janus_cli.config import load_config
            config = load_config()
        lessons = (config.get("learning", {}) or {}).get("lessons", {}) if isinstance(config, dict) else {}
        v = lessons.get("recency_half_life_days", _RECENCY_HALF_LIFE_DAYS)
        return float(v) if v is not None else _RECENCY_HALF_LIFE_DAYS
    except Exception:
        return _RECENCY_HALF_LIFE_DAYS


def _recency_decay(ts: str, *, now=None, half_life_days: float = _RECENCY_HALF_LIFE_DAYS) -> float:
    """Multiplicative recency weight in (0, 1]. 1.0 for now / unparseable / decay-off."""
    if not ts or half_life_days <= 0:
        return 1.0
    try:
        from datetime import datetime
        t = datetime.fromisoformat(ts)
        if now is None:
            from janus_time import now as _jnow
            now = _jnow()
        age_days = (now - t).total_seconds() / 86400.0
        if age_days <= 0:
            return 1.0
        return 0.5 ** (age_days / half_life_days)
    except Exception:
        return 1.0


def recall_lessons(
    query: str, *, n: int = 3, now=None, half_life_days: Optional[float] = None
) -> List[Dict[str, Any]]:
    """Return up to ``n`` past lessons most relevant to ``query``, ranked.

    Lexical token overlap over the lesson text and its task type, decayed by a
    recency half-life so newer lessons win ties, and scaled by demonstrated
    EFFICACY (move 4) — a lesson that keeps riding with failed sessions sinks,
    a helpful one rises — with an optional semantic re-rank when an embedding
    backend is installed. ``now`` / ``half_life_days`` are injectable for testing.
    """
    qt = _tokens(query)
    if not qt:
        return []
    hl = _recency_half_life_days() if half_life_days is None else half_life_days
    scored: List[Dict[str, Any]] = []
    for rec in load():
        text = str(rec.get("lesson", ""))
        et = _tokens(text) | _tokens(str(rec.get("task_type", "")))
        if not et:
            continue
        overlap = len(qt & et)
        if overlap == 0:
            continue
        score = (overlap / (len(et) ** 0.5)) * _recency_decay(
            rec.get("ts", ""), now=now, half_life_days=hl
        ) * _efficacy_multiplier(rec)
        scored.append({
            "id": rec.get("id", ""),
            "lesson": text,
            "task_type": rec.get("task_type", "general"),
            "ts": rec.get("ts", ""),
            "score": round(score, 4),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    try:
        from agent.embeddings import hybrid_fuse
        return hybrid_fuse(query, scored, n, text_of=lambda h: h["lesson"])
    except Exception:
        return scored[:n]


def format_lessons_for_prompt(lessons: List[Dict[str, Any]]) -> str:
    """Render recalled lessons as a compact block for prompt injection."""
    if not lessons:
        return ""
    lines = ["Lessons from past mistakes on similar tasks:"]
    for h in lessons:
        tt = h.get("task_type") or "general"
        lines.append(f"- ({tt}) {h.get('lesson', '').strip()}")
    return "\n".join(lines)


def recall_injection_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """True unless ``learning.lessons.recall_injection`` is explicitly false."""
    try:
        if config is None:
            from janus_cli.config import load_config
            config = load_config()
        lessons_cfg = ((config.get("learning") or {}).get("lessons") or {})
        return bool(lessons_cfg.get("recall_injection", True))
    except Exception:
        return True


# Lessons are distilled by an aux model from session transcripts, which can
# contain untrusted third-party content — treat stored lesson text as tainted
# at the injection boundary. Mirror the memory path's defenses: strip fence
# tags so a crafted lesson cannot terminate its wrapper early and read as user
# input, and cap the block so an oversized record can't flood the turn.
_LESSON_FENCE_RE = re.compile(
    r"</?\s*(?:lessons-context|memory-context)\s*>", re.IGNORECASE)
_MAX_LESSONS_CONTEXT_CHARS = 4000


def recall_context_for_turn(query: str, *, n: int = 3, session_id: str = "") -> str:
    """Fenced lessons block for the current turn's user message, or ``""``.

    The push half of reflexion: instead of waiting for the model to call the
    recall_lessons tool, the conversation loop appends the top-``n`` relevant
    lessons to the current turn's user message. The block is PERSISTED as part
    of that message (never the system prompt): an API-copy-only injection made
    the turn's user bytes differ from their replay on every later turn,
    tearing the provider prompt-cache prefix at this message each turn. Pure
    local-file read — no aux calls. Empty when disabled, the query is blank,
    or nothing relevant is stored.

    When ``session_id`` is given, lessons already surfaced to this session are
    EXCLUDED (they're still in the conversation from the earlier turn —
    re-injecting them would only accrete duplicate persisted bytes), and the
    surfaced ids are logged so the session's eventual outcome can credit/debit
    them (move 4 efficacy loop).
    """
    try:
        if not str(query or "").strip() or not recall_injection_enabled():
            return ""
        hits = recall_lessons(query, n=n)
        if session_id:
            try:
                _seen = set(_load_recall_state().get(session_id, []))
                hits = [h for h in hits if h.get("id", "") not in _seen]
            except Exception:
                pass
        block = format_lessons_for_prompt(hits)
        if not block:
            return ""
        if session_id:
            log_recall(session_id, [h.get("id", "") for h in hits])
        block = _LESSON_FENCE_RE.sub("", block)
        if len(block) > _MAX_LESSONS_CONTEXT_CHARS:
            cut = block.rfind("\n", 0, _MAX_LESSONS_CONTEXT_CHARS)
            block = block[: cut if cut > 0 else _MAX_LESSONS_CONTEXT_CHARS].rstrip()
            block += "\n…[lessons truncated]"
        return (
            "<lessons-context>\n"
            "[Lessons this agent distilled from its own past sessions — "
            "reference, NOT new user input; apply if relevant.]\n\n"
            f"{block}\n"
            "</lessons-context>"
        )
    except Exception as exc:
        logger.debug("lesson recall injection failed: %s", exc)
        return ""


def stats() -> Dict[str, Any]:
    """Aggregate counts for the dashboard: total + per task-type."""
    records = load()
    by_type: Dict[str, int] = {}
    for r in records:
        tt = str(r.get("task_type", "general")) or "general"
        by_type[tt] = by_type.get(tt, 0) + 1
    return {"total": len(records), "by_task_type": by_type}
