"""Session-end procedure mining — draft reusable skills from successful work.

Where memory mining (``agent/memory_miner.py``) distills FACTS from a
transcript, this distills PROCEDURES: multi-step task patterns that succeeded
and are worth saving as a skill for next time. It writes **draft** skills under
``skills/.drafts/`` for the user to review — it never auto-activates them, so
the live skill library's quality can't silently degrade. The agent's
in-the-moment ``skill_manage(create)`` still works; this is the retrospective
safety net that catches the reusable procedures it missed while busy.

Pure + injectable (``llm_caller``) so it is fully testable without a model;
best-effort (never raises).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.memory_miner import _render_transcript  # shared transcript flattener

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You identify REUSABLE PROCEDURES in a conversation transcript: multi-step "
    "task patterns that SUCCEEDED and would be worth saving as a skill to reuse "
    "next time. Ignore chitchat, trivial single-step actions, and anything that "
    "failed or is too specific to be reused."
)


def _build_prompt(transcript: str, existing_names: List[str]) -> str:
    existing = ", ".join(sorted(existing_names)) or "(none)"
    return (
        "From the transcript, extract reusable procedures as a JSON array. Each "
        "item is an object with these keys:\n"
        '  "name": kebab-case id (e.g. "deploy-staging")\n'
        '  "description": one sentence, <= 60 chars, ends with a period\n'
        '  "category": short category (e.g. "devops", "research")\n'
        '  "when_to_use": one line on when to reach for this\n'
        '  "steps": array of concise imperative steps\n'
        '  "script_filename": optional, e.g. "run.py" (empty if none)\n'
        '  "script_content": optional Python helper if one clearly helps (empty if none)\n\n'
        "Only include procedures that genuinely succeeded and are repeatable. "
        f"Skip anything already covered by these existing skills: {existing}. "
        "If nothing qualifies, return []. Return ONLY the JSON array."
        f"\n\nTRANSCRIPT:\n{transcript}"
    )


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return s or "skill"


def _parse_proposals(raw: Optional[str]) -> List[Dict[str, Any]]:
    """Parse the model's JSON array reply, tolerating code fences / prose."""
    if not raw or not raw.strip():
        return []
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt).strip()
    match = re.search(r"\[.*\]", txt, re.DOTALL)
    if match:
        txt = match.group(0)
    try:
        data = json.loads(txt)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name", "")).strip()
        desc = str(item.get("description", "")).strip()
        steps = item.get("steps", [])
        if not raw_name or not desc or not isinstance(steps, list) or not steps:
            continue
        out.append({
            "name": _slug(raw_name),
            "description": desc[:60],
            "category": str(item.get("category", "general")).strip() or "general",
            "when_to_use": str(item.get("when_to_use", "")).strip(),
            "steps": [str(s).strip() for s in steps if str(s).strip()],
            "script_filename": str(item.get("script_filename", "")).strip(),
            "script_content": str(item.get("script_content", "")),
        })
    return out


def _red_team_proposals(
    proposals: List[Dict[str, Any]],
    transcript: str,
    existing_names: List[str],
    *,
    llm_caller: Optional[Callable[..., Any]] = None,
) -> tuple:
    """Run the dialectic admission gate over skill proposals.

    Returns ``(proposals, objections)`` where ``objections`` maps a proposal
    name to the skeptic's objection for rejected ones. Rejected proposals
    are NOT removed — drafts are the user-reviewed quarantine — they're
    flagged by the caller instead. Revised proposals get the arbiter's
    narrowed description. Infrastructure errors fail open (no objections).
    """
    from agent.deliberation import red_team_claims

    claims = [
        {
            "id": p["name"], "kind": "skill",
            "content": f"{p['description']} — when to use: {p['when_to_use']}; "
                       f"steps: {'; '.join(p['steps'][:6])}",
        }
        for p in proposals
    ]
    rt = red_team_claims(
        claims, transcript=transcript,
        existing="\n".join(existing_names),
        llm_caller=llm_caller,
    )
    if rt.get("error") is not None:
        return proposals, {}

    objections: Dict[str, str] = {}
    for p in proposals:
        v = rt["verdicts"].get(p["name"])
        if v is None:
            continue
        if v["verdict"] == "reject":
            objections[p["name"]] = v.get("skeptic_objection") or v.get("crux") or ""
            logger.info("red-team flagged skill draft %r: %s", p["name"], objections[p["name"]])
        elif v["verdict"] == "revise" and v.get("revised_content"):
            p["description"] = v["revised_content"][:60]
    return proposals, objections


def render_skill_md(proposal: Dict[str, Any]) -> str:
    """Build a SKILL.md (frontmatter + body) from a proposal."""
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(proposal["steps"], 1))
    script_line = ""
    if proposal.get("script_filename") and proposal.get("script_content"):
        script_line = (
            f"\nA helper script is included at `scripts/{proposal['script_filename']}` "
            "— run it with the `terminal` tool.\n"
        )
    return (
        "---\n"
        f"name: {proposal['name']}\n"
        f"description: {proposal['description']}\n"
        "version: 0.1.0\n"
        "author: Janus (mined draft)\n"
        "license: MIT\n"
        "metadata:\n"
        "  janus:\n"
        f"    category: {proposal['category']}\n"
        "    created_by: agent-draft\n"
        "---\n\n"
        f"# {proposal['name']} Skill\n\n"
        "Draft mined from a successful session. Review, refine, and move out of "
        "`.drafts/` to activate.\n\n"
        "## When to Use\n\n"
        f"{proposal.get('when_to_use') or proposal['description']}\n\n"
        "## Procedure\n\n"
        f"{steps}\n"
        f"{script_line}"
    )


def write_skill_draft(proposal: Dict[str, Any], drafts_dir: Optional[Path] = None) -> Path:
    """Write a draft skill (SKILL.md + optional script) under skills/.drafts/.

    Returns the skill directory. A clashing name gets a ``-2``, ``-3`` suffix so
    repeated mining never overwrites an earlier draft.
    """
    if drafts_dir is None:
        from janus_constants import get_janus_home
        drafts_dir = get_janus_home() / "skills" / ".drafts"
    base = proposal["name"]
    target = drafts_dir / base
    n = 2
    while target.exists():
        target = drafts_dir / f"{base}-{n}"
        n += 1
    try:
        from agent import skill_versions
        skill_versions.record_version(
            target.name, target, source=skill_versions.SOURCE_SKILL_MINER,
            reason="draft mined from a session",
        )
    except Exception:
        logger.debug("skill version record failed for draft %s", target.name, exc_info=True)
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(render_skill_md(proposal), encoding="utf-8")
    if proposal.get("script_filename") and proposal.get("script_content"):
        scripts = target / "scripts"
        scripts.mkdir(exist_ok=True)
        fname = _slug(proposal["script_filename"].rsplit(".", 1)[0]) + ".py"
        (scripts / fname).write_text(proposal["script_content"], encoding="utf-8")
    return target


def mine_session_skills(
    messages: List[Dict[str, Any]],
    *,
    llm_caller: Optional[Callable[..., Any]] = None,
    existing_skill_names: Optional[List[str]] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_skills: int = 3,
    write_drafts: bool = True,
    drafts_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Mine reusable procedures from ``messages`` and (optionally) write drafts.

    Returns ``{"proposals": [...], "written": [paths], "error": None|str}``.
    Never raises.
    """
    result: Dict[str, Any] = {"proposals": [], "written": [], "error": None}
    existing = {n.strip().lower() for n in (existing_skill_names or []) if n}
    try:
        transcript = _render_transcript(messages)
        if not transcript.strip():
            return result
        if llm_caller is None:
            from agent.auxiliary_client import call_llm as llm_caller
        response = llm_caller(
            task="skill_mining",
            provider=provider,
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _build_prompt(transcript, sorted(existing))},
            ],
            temperature=0,
            max_tokens=1500,
        )
        raw = response.choices[0].message.content
        proposals = [
            p for p in _parse_proposals(raw)
            if p["name"].lower() not in existing
        ][:max_skills]
        result["flagged"] = []  # drafts that failed the verification gate

        # Optional dialectic admission gate (learning.dialectic.* — off by
        # default). Rejected proposals are still written as drafts (drafts
        # are the user-reviewed quarantine) but carry the skeptic's
        # objection in result["flagged"]; revised ones get the arbiter's
        # narrowed description. Infrastructure errors fail open.
        from agent.deliberation import dialectic_enabled
        red_team_objections: Dict[str, str] = {}
        if dialectic_enabled("skills") and proposals:
            proposals, red_team_objections = _red_team_proposals(
                proposals, transcript, sorted(existing), llm_caller=llm_caller,
            )
        result["proposals"] = proposals
        if write_drafts:
            from agent.skill_verifier import verify_skill_dir
            for p in proposals:
                try:
                    path = write_skill_draft(p, drafts_dir=drafts_dir)
                    result["written"].append(str(path))
                    verdict = verify_skill_dir(path)
                    if not verdict["ok"]:
                        result["flagged"].append({"path": str(path), "issues": verdict["issues"]})
                    objection = red_team_objections.get(p["name"])
                    if objection:
                        result["flagged"].append({
                            "path": str(path),
                            "issues": [f"red-team objection: {objection}"],
                        })
                except Exception as exc:
                    logger.debug("draft write failed for %s: %s", p.get("name"), exc)
    except Exception as exc:  # mining must never break a session
        logger.debug("session skill mining failed: %s", exc)
        result["error"] = str(exc)
    return result
