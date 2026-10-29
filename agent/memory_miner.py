"""Session-end memory mining.

Distill a conversation transcript into durable memory facts via a cheap
auxiliary model, then write them to MEMORY.md / USER.md (and, transitively,
the dated daily journal). Inspired by OpenClaw's session mining: where Janus's
built-in memory is *active-only* (the agent saves a fact when it decides to),
this recovers durable facts the agent never explicitly saved.

Design:
- Pure + injectable: ``mine_session_memory`` takes an ``llm_caller`` so it is
  fully testable without a live model.
- Best-effort: any failure is swallowed and reported in the result dict — a
  mining failure must never break a session.
- De-duplicated: new facts are normalized and skipped if they already exist
  (exact / substring either direction) so repeated sessions don't pile up.
- Writes go through ``MemoryStore.add`` so the daily-snapshot journal and the
  char-limit guards apply automatically.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_MINING_SYSTEM = (
    "You extract DURABLE memory from a conversation transcript. Keep only "
    "facts worth remembering across future sessions: stable user preferences, "
    "identity, environment, project conventions, decisions, and tool quirks. "
    "EXCLUDE transient task details, one-off requests, and anything ephemeral."
)


def _build_prompt(transcript: str) -> str:
    return (
        "From the transcript below, extract durable memory as JSON with two "
        'string arrays: "user" (facts about the user — preferences, identity, '
        'environment, communication style) and "memory" (project facts, '
        "decisions, conventions, tool quirks). Each item is one concise "
        "sentence. Omit transient or one-off details. If nothing is worth "
        'saving, return {"user": [], "memory": []}. Return ONLY the JSON.\n\n'
        f"TRANSCRIPT:\n{transcript}"
    )


def _render_transcript(messages: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    """Flatten user+assistant text turns; keep the most recent ``max_chars``."""
    parts: List[str] = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        parts.append(f"{m['role'].upper()}: {content.strip()}")
    text = "\n\n".join(parts)
    return text[-max_chars:]


def _normalize(s: str) -> str:
    return " ".join(s.lower().split())


def _is_duplicate(content: str, existing_norm: List[str]) -> bool:
    n = _normalize(content)
    if not n:
        return True
    for e in existing_norm:
        if n == e or n in e or e in n:
            return True
    return False


def _parse_facts(raw: Optional[str]) -> Dict[str, List[str]]:
    """Parse the model's JSON reply, tolerating code fences / surrounding prose."""
    empty = {"user": [], "memory": []}
    if not raw or not raw.strip():
        return empty
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt).strip()
    match = re.search(r"\{.*\}", txt, re.DOTALL)
    if match:
        txt = match.group(0)
    try:
        data = json.loads(txt)
    except (ValueError, TypeError):
        return empty
    if not isinstance(data, dict):
        return empty
    out = {"user": [], "memory": []}
    for key in ("user", "memory"):
        v = data.get(key, [])
        if isinstance(v, list):
            out[key] = [str(x).strip() for x in v if str(x).strip()]
    return out


def _red_team_facts(
    facts: Dict[str, List[str]],
    transcript: str,
    *,
    llm_caller: Optional[Callable[..., Any]] = None,
) -> tuple:
    """Run the dialectic admission gate over candidate facts.

    Returns ``(surviving_facts, summary_or_None)``. On infrastructure error
    the original facts pass through unchanged (fail open) with summary None.
    Rejected facts are kept in the summary with the skeptic's objection so
    nothing is silently destroyed.
    """
    from agent.deliberation import apply_verdicts, red_team_claims

    claims = []
    for target in ("user", "memory"):
        for i, content in enumerate(facts.get(target, [])):
            claims.append({
                "id": f"{target}-{i}", "kind": "fact",
                "content": content, "context": f"{target} memory",
            })
    rt = red_team_claims(claims, transcript=transcript, llm_caller=llm_caller)
    if rt.get("error") is not None or not claims:
        return facts, None

    split = apply_verdicts(claims, rt["verdicts"])
    surviving: Dict[str, List[str]] = {"user": [], "memory": []}
    for c in split["accepted"]:
        target = str(c["id"]).rsplit("-", 1)[0]
        surviving[target].append(c["content"])
    rejected = [
        {"content": c["content"], "objection": c.get("objection", "")}
        for c in split["rejected"]
    ]
    for r in rejected:
        logger.info("red-team rejected fact: %r — %s", r["content"], r["objection"])
    summary = {
        "accepted": len(split["accepted"]),
        "rejected": rejected,
        "calls": rt.get("calls", 0),
    }
    return surviving, summary


def mine_session_memory(
    messages: List[Dict[str, Any]],
    memory_store: Any,
    *,
    llm_caller: Optional[Callable[..., Any]] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_facts_per_target: int = 12,
) -> Dict[str, Any]:
    """Distill durable facts from ``messages`` into ``memory_store``.

    Returns a summary dict: ``{"added": {"user": N, "memory": M},
    "skipped_duplicates": K, "error": None|str}``. Never raises.
    """
    result: Dict[str, Any] = {
        "added": {"user": 0, "memory": 0},
        "skipped_duplicates": 0,
        "error": None,
    }
    try:
        transcript = _render_transcript(messages)
        if not transcript.strip():
            return result

        if llm_caller is None:
            from agent.auxiliary_client import call_llm as llm_caller

        # ACE playbook: prepend any learned guidance for the memory-mining step
        # (no-op when the playbook is disabled/empty — learning.playbook.enabled).
        from agent.playbook import augment_system
        system_prompt = augment_system(_MINING_SYSTEM, "memory")

        response = llm_caller(
            task="memory_mining",
            provider=provider,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _build_prompt(transcript)},
            ],
            temperature=0,
            max_tokens=1000,
        )
        raw = response.choices[0].message.content
        facts = _parse_facts(raw)

        # Optional dialectic admission gate (learning.dialectic.* — off by
        # default): an advocate/skeptic/arbiter exchange rules on each
        # candidate fact before it reaches the trusted store. Infrastructure
        # errors fail open to today's behavior; real rejections are reported
        # in result["red_team"], never silently dropped.
        from agent.deliberation import dialectic_enabled
        if dialectic_enabled("memory") and any(facts.get(t) for t in ("user", "memory")):
            facts, red_team = _red_team_facts(facts, transcript, llm_caller=llm_caller)
            if red_team is not None:
                result["red_team"] = red_team

        existing = {
            "user": [_normalize(e) for e in getattr(memory_store, "user_entries", [])],
            "memory": [_normalize(e) for e in getattr(memory_store, "memory_entries", [])],
        }
        # Provenance (memory.provenance): mined facts are tagged as such, and as
        # untrusted when the transcript carried fenced external content.
        from agent.memory_provenance import provenance_context, transcript_has_untrusted
        with provenance_context("miner", untrusted=transcript_has_untrusted(messages)):
            _mine_write_facts(facts, memory_store, existing, result, llm_caller=llm_caller,
                              provider=provider, model=model,
                              max_facts_per_target=max_facts_per_target)
    except Exception as exc:  # mining must never break a session
        logger.debug("session memory mining failed: %s", exc)
        result["error"] = str(exc)
    return result


def _mine_write_facts(facts, memory_store, existing, result, *, llm_caller, provider, model,
                      max_facts_per_target):
    """Write mined facts to the store (split out so provenance can wrap it)."""
    from agent.feature_flags import flag_enabled
    reconcile_on = flag_enabled("memory", "write_time_reconcile", default=False)

    for target in ("user", "memory"):
        # `live` holds RAW entry text (passed to reconcile_candidate and
        # indexed for removal); existing[target] holds NORMALIZED text (for
        # _is_duplicate). They are intentionally different representations.
        live = list(getattr(memory_store, f"{target}_entries", []))
        for content in facts.get(target, [])[:max_facts_per_target]:
            content = content.strip()
            if not content:
                continue
            if _is_duplicate(content, existing[target]):
                result["skipped_duplicates"] += 1
                continue

            if reconcile_on and live:
                from agent.memory_gardener import reconcile_candidate
                decision = reconcile_candidate(content, live, llm_caller=llm_caller,
                                               provider=provider, model=model)
                action = decision["action"]
                idx = decision.get("target_index")
                if action == "NOOP":
                    result["skipped_duplicates"] += 1
                    continue
                if action in ("UPDATE", "DELETE") and idx is not None and 0 <= idx < len(live):
                    old = live[idx]
                    try:
                        memory_store.remove(target, old)
                    except Exception as exc:
                        logger.debug("reconcile remove failed: %s", exc)
                    live.pop(idx)
                    result.setdefault("reconciled", {"updated": 0, "deleted": 0})
                    if action == "DELETE":
                        result["reconciled"]["deleted"] += 1
                        existing[target] = [e for e in existing[target] if e != _normalize(old)]
                        continue
                    result["reconciled"]["updated"] += 1
                    existing[target] = [e for e in existing[target] if e != _normalize(old)]

            add_result = memory_store.add(target, content)
            if isinstance(add_result, dict) and add_result.get("success"):
                result["added"][target] += 1
                existing[target].append(_normalize(content))
                live.append(content)
            else:
                result["skipped_duplicates"] += 1
