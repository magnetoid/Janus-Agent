"""Governed self-improvement (DGM-lite) — move 9.

Lets Janus propose variants of its OWN artifacts — skill drafts, prompt
fragments, lesson-curation policies — and promote the ones that measurably help.
This is the pattern that compounds in the research (Darwin Gödel Machine, SICA)
but ONLY under a hard-to-game evaluator plus an archive of what worked: DGM
fabricated test logs and stripped its own safety markers the moment its harness
was in reach, so the gates below are the whole point, not decoration.

NON-NEGOTIABLE GUARDRAILS (a proposal that violates any is refused, never applied):
  1. NEVER core code. A target must resolve UNDER $JANUS_HOME and inside the
     self-artifact allowlist (skills/.drafts, prompts, policies). Absolute paths,
     ``..`` escapes, and anything in the repo/core are refused.
  2. Disabled by default (``learning.self_improve.enabled``).
  3. Promotion requires ALL of: an evaluation that did NOT regress the eval suite
     (move 2), the self-improvement governor NOT frozen, the autonomy safety
     floor (move 3) NOT engaged, and explicit human approval — unless trust is
     deliberately graduated in config.
  4. Every apply takes a backup; ``rollback`` restores it. A full lineage archive
     (parent → child + eval evidence) is kept so improvements compound and any
     regression is traceable to its origin.

The data + gate layer is pure and best-effort; the eval scores and the actual
apply are the two seams an orchestrator (or a test) drives, so the
safety-critical promotion gate is verifiable without a live model or real eval
execution.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ALLOWED_KINDS = ("skill", "prompt_fragment", "lesson_policy")
# Target paths (relative to $JANUS_HOME) may only live under these roots. Skill
# variants go to the drafts dir — they still face the curator's own promotion
# gate afterwards, so this never bypasses skill graduation.
_ALLOWED_ROOTS: Tuple[Tuple[str, ...], ...] = (
    ("skills", ".drafts"),
    ("prompts",),
    ("policies",),
)
_VALID_STATUSES = ("proposed", "evaluated", "approved", "promoted", "rejected", "rolled_back")


def archive_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "self_improve.json"


def _now_iso() -> str:
    try:
        from janus_time import now as _now
        return _now().isoformat()
    except Exception:
        return ""


# --- config gates -----------------------------------------------------------

def _cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        if config is None:
            from janus_cli.config import load_config
            config = load_config()
        si = ((config.get("learning") or {}).get("self_improve") or {})
        return si if isinstance(si, dict) else {}
    except Exception:
        return {}


def enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    return bool(_cfg(config).get("enabled", False))


def require_human_approval(config: Optional[Dict[str, Any]] = None) -> bool:
    # Default True: a missing/garbled flag must NOT auto-approve self-modification.
    return bool(_cfg(config).get("require_human_approval", True))


# --- target validation (guardrail 1) ----------------------------------------

def resolve_target(target: str) -> Optional[Path]:
    """Resolve a proposal target (relative to $JANUS_HOME) to an absolute path,
    or None if it escapes $JANUS_HOME or falls outside the self-artifact
    allowlist. This is the line between self-artifacts and core code."""
    try:
        from janus_constants import get_janus_home
        root = get_janus_home().resolve()
        if not str(target).strip():
            return None
        # An absolute target joins as-is (Path drops root), so an escape resolves
        # outside root and is caught below; ../ escapes likewise.
        p = (root / str(target)).resolve()
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            return None  # escaped $JANUS_HOME
        for allowed in _ALLOWED_ROOTS:
            if rel_parts[:len(allowed)] == allowed and len(rel_parts) > len(allowed):
                return p
        return None
    except Exception:
        return None


def _has_symlink_component(path: Path, root: Path) -> bool:
    """True if any EXISTING component from ``root`` down to ``path`` is a symlink.
    resolve_target() dereferences symlinks at check time; this closes most of the
    check→write TOCTOU window (CWE-367) by refusing when a symlink was planted in
    the path — combined with O_NOFOLLOW on the final open below."""
    try:
        cur = root
        for part in path.relative_to(root).parts:
            cur = cur / part
            if cur.is_symlink():
                return True
        return False
    except Exception:
        return True  # cannot verify → treat as unsafe (fail closed)


def _safe_write(path: Path, data: bytes, root: Path) -> None:
    """Write ``data`` to ``path`` refusing to follow symlinks — no path component
    may be a symlink, and the final component is opened O_NOFOLLOW. Defends the
    'never escapes the allowlist' guarantee against a symlink swapped in after
    validation (the marginal residue is a parent-dir swap in the tiny mkdir→open
    window; the agent would need a separate FS primitive to exploit it)."""
    if _has_symlink_component(path, root):
        raise OSError(f"refusing write: symlink in target path {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _safe_read_bytes(path: Path, root: Path) -> bytes:
    """Read ``path`` refusing to traverse/follow symlinks (for the backup copy)."""
    if _has_symlink_component(path, root):
        raise OSError(f"refusing read: symlink in path {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        return os.read(fd, 16_000_000)
    finally:
        os.close(fd)


# --- archive ----------------------------------------------------------------

def load_archive() -> List[Dict[str, Any]]:
    p = archive_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _save_archive(records: List[Dict[str, Any]]) -> None:
    p = archive_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get(proposal_id: str) -> Optional[Dict[str, Any]]:
    return next((r for r in load_archive() if r.get("id") == proposal_id), None)


def _update(proposal_id: str, **fields) -> Optional[Dict[str, Any]]:
    records = load_archive()
    hit = None
    for r in records:
        if r.get("id") == proposal_id:
            r.update(fields)
            r["updated_at"] = _now_iso()
            hit = r
            break
    if hit is not None:
        _save_archive(records)
    return hit


def _next_id(records: List[Dict[str, Any]]) -> str:
    # Deterministic, monotonic — no Math.random / clock needed for the id.
    return f"si-{len(records) + 1:04d}"


def lineage(proposal_id: str) -> List[Dict[str, Any]]:
    """Chain from the root ancestor down to ``proposal_id`` (oldest first)."""
    by_id = {r.get("id"): r for r in load_archive()}
    chain: List[Dict[str, Any]] = []
    seen = set()
    cur = by_id.get(proposal_id)
    while cur and cur.get("id") not in seen:
        seen.add(cur.get("id"))
        chain.append(cur)
        cur = by_id.get(cur.get("parent_id"))
    return list(reversed(chain))


# --- propose / evaluate / approve / promote / rollback ----------------------

def propose(kind: str, target: str, content: str, *,
            rationale: str = "", parent_id: Optional[str] = None,
            config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Record a proposed self-modification (status=proposed), or None if refused.

    Refused when self-improvement is disabled, the kind is unknown, or the target
    is not an allowlisted self-artifact path (guardrail 1). Recording a proposal
    NEVER writes the target — that only happens at ``promote`` past every gate.
    """
    try:
        if not enabled(config):
            logger.debug("self_improve.propose refused: disabled")
            return None
        if kind not in ALLOWED_KINDS:
            logger.warning("self_improve.propose refused: unknown kind %r", kind)
            return None
        if resolve_target(target) is None:
            logger.warning("self_improve.propose REFUSED: target %r is not an "
                           "allowlisted self-artifact (core code is never a target)", target)
            return None
        records = load_archive()
        rec = {
            "id": _next_id(records),
            "parent_id": parent_id or "",
            "kind": kind,
            "target": str(target),
            "content": str(content),
            "rationale": str(rationale),
            "status": "proposed",
            "score_before": None,
            "score_after": None,
            "gate_ok": False,
            "approved_by": "",
            "backup": "",
            "ts": _now_iso(),
        }
        records.append(rec)
        _save_archive(records)
        return rec
    except Exception as exc:
        logger.debug("self_improve.propose failed: %s", exc)
        return None


def record_evaluation(proposal_id: str, *, score_before: float, score_after: float,
                      gate_ok: Optional[bool] = None,
                      detail: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Attach eval evidence and move to status=evaluated.

    ``gate_ok`` defaults to a STRICT improvement (score_after > score_before): a
    self-modification promotes only when it measurably helps, so a no-op or a
    fabricated 0→0 pair does NOT pass. The eval orchestrator passes an explicit
    noise-aware verdict instead (multi-trial means, per-eval improvement floor,
    regression-flip veto — see ``eval_orchestrator._compute_gate``) plus a
    ``detail`` dict stored as ``eval_detail`` so `self-improve show`/doctor can
    display *which* evals moved. Scores are computed with the variant applied in
    an ISOLATED profile — never the live one. Refuses to re-evaluate a PROMOTED
    proposal (that would let a re-promote clobber the rollback backup); rollback
    first to re-apply.
    """
    try:
        rec = get(proposal_id)
        if rec is None:
            return None
        if rec.get("status") == "promoted":
            logger.warning("self_improve: refusing to re-evaluate promoted %s "
                           "(rollback first)", proposal_id)
            return None
        ok = (score_after > score_before) if gate_ok is None else bool(gate_ok)
        extra: Dict[str, Any] = {}
        if detail is not None:
            extra["eval_detail"] = detail
        return _update(proposal_id, status="evaluated",
                       score_before=float(score_before), score_after=float(score_after),
                       gate_ok=ok, **extra)
    except Exception as exc:
        logger.debug("self_improve.record_evaluation failed: %s", exc)
        return None


def approve(proposal_id: str, *, by: str = "human") -> Optional[Dict[str, Any]]:
    """Human sign-off. Only an EVALUATED proposal can be approved (you can't
    approve something whose eval evidence doesn't exist yet)."""
    rec = get(proposal_id)
    if rec is None or rec.get("status") != "evaluated":
        return None
    return _update(proposal_id, status="approved", approved_by=by or "human")


def reject(proposal_id: str, *, reason: str = "") -> Optional[Dict[str, Any]]:
    rec = get(proposal_id)
    new_rationale = reason or (rec.get("rationale", "") if rec else "")
    return _update(proposal_id, status="rejected", rationale=new_rationale)


def record_review(proposal_id: str, *, verdict: str, objection: str = "",
                  by: str = "twin-core") -> Optional[Dict[str, Any]]:
    """Attach an adversarial second-core review to a proposal as METADATA only.

    This is the write seam for the twin-core reviewer (agent/twin_review.py). It
    is deliberately incapable of advancing a proposal: it never touches
    ``status``, ``gate_ok``, or ``approved_by``. The reviewer core may VETO (the
    caller separately invokes ``reject``) but the mutual-approval path is banned —
    a second LLM must never be able to sign off a self-modification, only a human
    can. Recording a review here is therefore inert with respect to the promotion
    gate."""
    return _update(proposal_id, review={
        "verdict": str(verdict), "objection": str(objection),
        "by": str(by), "at": _now_iso(),
    })


def can_promote(proposal_id: str, config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """The gate. True only when every safety condition holds. Returns (ok, reason)."""
    try:
        if not enabled(config):
            return False, "self-improvement is disabled (learning.self_improve.enabled)"
        rec = get(proposal_id)
        if rec is None:
            return False, "no such proposal"
        if rec.get("status") not in ("evaluated", "approved"):
            return False, f"not evaluated yet (status={rec.get('status')})"
        if not rec.get("gate_ok"):
            return False, "evaluation did not show a measured improvement"
        if resolve_target(rec.get("target", "")) is None:
            return False, "target failed re-validation (not an allowlisted self-artifact)"
        # The eval suite must have real statistical teeth before ANY promotion:
        # a gate that passes on a tiny (or empty) suite is fabricated evidence
        # with extra steps. Fail CLOSED on error.
        try:
            min_specs = int(_cfg(config).get("min_eval_specs", 10) or 0)
        except Exception:
            min_specs = 10
        try:
            from agent.evals import load_eval_specs
            n_specs = len(load_eval_specs())
        except Exception:
            n_specs = 0
        if n_specs < min_specs:
            return False, (
                f"eval suite too small ({n_specs} < {min_specs} specs): "
                "run 'janus evals init' to seed the bundled suite")
        # Cross-check the REAL move-2 spine: the live eval suite must not be in a
        # recorded regression right now. This ties promotion to the actual
        # measurement, so caller-supplied per-proposal scores are not the only
        # evidence (DGM's fabricated-evidence failure mode). fail_closed=True:
        # "not enough eval history" REFUSES instead of passing vacuously — run
        # `janus evals trend` twice on the current suite first.
        try:
            from agent.eval_trend import regression_gate
            gate = regression_gate(fail_closed=True)
            if not gate.get("ok", True):
                return False, f"live eval suite gate refused: {gate.get('message')}"
        except Exception as exc:
            return False, f"eval-gate cross-check unavailable (refusing): {exc}"
        # Governor freeze (move 2/3.2 health signal). Safety gate → fail CLOSED.
        try:
            from agent.self_improvement_governor import assess_admission_state, STATE_FROZEN
            if assess_admission_state().get("state") == STATE_FROZEN:
                return False, "the self-improvement governor is FROZEN"
        except Exception as exc:
            return False, f"governor check unavailable (refusing): {exc}"
        # Autonomy safety floor (move 3): freeze / spend cap. Fail CLOSED.
        try:
            from agent.autonomy_guard import blocked_reason
            br = blocked_reason(config)
            if br:
                return False, f"autonomy safety floor: {br}"
        except Exception as exc:
            return False, f"autonomy check unavailable (refusing): {exc}"
        # Human approval, unless trust is deliberately graduated.
        if require_human_approval(config) and rec.get("status") != "approved":
            return False, "awaiting human approval"
        return True, "ok"
    except Exception as exc:
        return False, f"gate error (refusing): {exc}"


def promote(proposal_id: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Apply the variant to its target IFF ``can_promote``. Backs up any existing
    file first (for rollback), then writes the new content. Refuses otherwise."""
    ok, reason = can_promote(proposal_id, config)
    if not ok:
        return {"promoted": False, "reason": reason}
    rec = get(proposal_id) or {}
    if rec.get("status") == "promoted":
        return {"promoted": False, "reason": "already promoted (rollback first to re-apply)"}
    from janus_constants import get_janus_home
    root = get_janus_home().resolve()
    target = resolve_target(rec.get("target", ""))
    if target is None:
        return {"promoted": False, "reason": "target failed re-validation"}
    try:
        if rec.get("kind") == "skill":
            return _promote_skill(proposal_id, rec, root, target)
        # NEVER overwrite an existing backup — that would destroy the original
        # content and break rollback. Only take a backup on the first apply.
        backup = rec.get("backup") or ""
        if not backup and target.exists():
            bpath = target.with_name(target.name + f".bak-{proposal_id}")
            _safe_write(bpath, _safe_read_bytes(target, root), root)
            backup = str(bpath)
        _safe_write(target, str(rec.get("content", "")).encode("utf-8"), root)
        _update(proposal_id, status="promoted", backup=backup)
        _audit_promoted(proposal_id, str(target), backup)
        logger.info("self_improve: promoted %s → %s", proposal_id, target)
        return {"promoted": True, "target": str(target), "backup": backup or None}
    except Exception as exc:
        logger.warning("self_improve.promote apply failed: %s", exc)
        return {"promoted": False, "reason": f"apply failed: {exc}"}


def _audit_promoted(proposal_id: str, applied_to: str, backup: str) -> None:
    try:
        from agent.audit_log import append_event
        append_event("self_improve_promoted",
                     {"proposal_id": proposal_id, "applied_to": applied_to,
                      "backup": backup})
    except Exception:
        pass


def _promote_skill(proposal_id: str, rec: Dict[str, Any], root: Path,
                   draft_target: Path) -> Dict[str, Any]:
    """Apply an approved skill variant so it actually goes LIVE.

    The proposal *target* (what an LLM may name) stays confined to
    ``_ALLOWED_ROOTS`` — always ``skills/.drafts/...``. The apply path is
    derived HERE, post-gate, by resolving the active skill's SKILL.md by
    frontmatter name; it is still strictly under ``$JANUS_HOME/skills`` and
    every write goes through ``_safe_write`` (symlink refusal). Historically
    ``promote()`` wrote only the draft file itself — a directory nothing
    loads — so a fully approved self-modification changed nothing.
    """
    from agent.skill_utils import (
        draft_skill_name_from_target, parse_frontmatter, resolve_active_skill_md,
    )
    name = draft_skill_name_from_target(rec.get("target", ""))
    if not name:
        return {"promoted": False, "reason": "could not resolve skill name from target"}
    skills_root = root / "skills"
    content = str(rec.get("content", ""))

    # Batch-level tar.gz snapshot before the first live mutation, so
    # `janus curator rollback` can undo the promotion wholesale (same pattern
    # as auto_promote_drafts). Best-effort — the per-file backup below is the
    # promotion's own rollback anchor.
    try:
        from agent.curator_backup import snapshot_skills
        snapshot_skills(reason=f"pre-self-improve-promote-{proposal_id}")
    except Exception:
        logger.debug("pre-promote snapshot failed (continuing)", exc_info=True)

    active_md = resolve_active_skill_md(skills_root, name)
    if active_md is not None:
        # In-place replacement of the live skill.
        backup = rec.get("backup") or ""
        if not backup:
            bpath = active_md.with_name(active_md.name + f".bak-{proposal_id}")
            _safe_write(bpath, _safe_read_bytes(active_md, root), root)
            backup = str(bpath)
        _safe_write(active_md, content.encode("utf-8"), root)
        _update(proposal_id, status="promoted", backup=backup,
                applied_to=str(active_md), applied_mode="in_place")
        _audit_promoted(proposal_id, str(active_md), backup)
        logger.info("self_improve: promoted %s in place → %s", proposal_id, active_md)
        return {"promoted": True, "target": str(active_md), "backup": backup or None}

    # Net-new skill (no active copy): write the draft, then activate it into
    # the live tree via the graph's non-clobbering activation move. Any
    # pre-existing draft content is backed up OUTSIDE the moved directory
    # (the .drafts root survives the move) so rollback can restore it.
    backup = rec.get("backup") or ""
    if not backup and draft_target.exists():
        bpath = draft_target.parent.parent / f"{name}.SKILL.md.bak-{proposal_id}"
        _safe_write(bpath, _safe_read_bytes(draft_target, root), root)
        backup = str(bpath)
    _safe_write(draft_target, content.encode("utf-8"), root)
    try:
        fm, _ = parse_frontmatter(content)
    except Exception:
        fm = {}
    try:
        from agent.skill_graph import _activate_draft
        moved = _activate_draft(draft_target.parent, name, fm or {}, set())
    except Exception as exc:
        logger.warning("self_improve: draft activation failed: %s", exc)
        moved = None
    if moved is None:
        return {"promoted": False,
                "reason": "activation failed (draft written but not active)"}
    applied = resolve_active_skill_md(skills_root, moved) or (
        skills_root / moved / "SKILL.md")
    _update(proposal_id, status="promoted", backup=backup,
            applied_to=str(applied), applied_mode="activated")
    _audit_promoted(proposal_id, str(applied), backup)
    logger.info("self_improve: promoted %s as new skill → %s", proposal_id, applied)
    return {"promoted": True, "target": str(applied), "backup": backup or None}


def rollback(proposal_id: str) -> Dict[str, Any]:
    """Undo a promoted variant: restore the backup, or remove the file if the
    target hadn't existed before. Idempotent-ish; best-effort."""
    rec = get(proposal_id)
    if rec is None:
        return {"rolled_back": False, "reason": "no such proposal"}
    from janus_constants import get_janus_home
    root = get_janus_home().resolve()
    target = resolve_target(rec.get("target", ""))
    # Skill promotions record where they actually landed (``applied_to``) plus
    # how (``applied_mode``): "in_place" replaced a live skill's SKILL.md,
    # "activated" moved a net-new draft into the active tree. Old records
    # without the fields fall back to the legacy draft-target restore.
    applied = str(rec.get("applied_to") or "")
    mode = str(rec.get("applied_mode") or "")
    applied_path: Optional[Path] = None
    if applied:
        try:
            p = Path(applied)
            p.resolve().relative_to((root / "skills").resolve())
            applied_path = p
        except (ValueError, OSError):
            return {"rolled_back": False, "reason": "applied_to escapes skills dir"}
    try:
        backup = rec.get("backup") or ""
        if mode == "activated" and applied_path is not None:
            # Undo the activation: remove the live file (+ now-empty dir),
            # then restore any pre-existing draft content from backup.
            if applied_path.exists() and not applied_path.is_symlink():
                applied_path.unlink()
                try:
                    if not any(applied_path.parent.iterdir()):
                        applied_path.parent.rmdir()
                except OSError:
                    pass
            if backup and Path(backup).exists() and target is not None:
                _safe_write(target, _safe_read_bytes(Path(backup), root), root)
                Path(backup).unlink()
            _update(proposal_id, status="rolled_back", backup="")
            return {"rolled_back": True, "target": str(applied_path)}
        if applied_path is not None:
            target = applied_path  # in_place: restore at the live skill's path
        if target is None:
            return {"rolled_back": False, "reason": "bad target"}
        if backup and Path(backup).exists():
            _safe_write(target, _safe_read_bytes(Path(backup), root), root)
            Path(backup).unlink()
        elif target.exists() and not target.is_symlink():
            target.unlink()  # nothing existed before → remove what we added
        _update(proposal_id, status="rolled_back", backup="")
        return {"rolled_back": True, "target": str(target)}
    except Exception as exc:
        return {"rolled_back": False, "reason": f"restore failed: {exc}"}
