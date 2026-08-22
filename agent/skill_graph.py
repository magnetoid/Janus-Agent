"""Skill graph + verifiable-reward promotion.

Upgrades skills from a flat list to a directed ACYCLIC graph: nodes are skills,
edges are prerequisite/composition relationships, and each node carries a
``promotion_level`` (a quality TIER — orthogonal to the curator's lifecycle
state of active/stale/archived) plus a ``refinement_flagged`` mark.

Promotion is gated by VERIFIABLE rewards, not raw usage: a skill is promotable
only when its static self-test passes (agent/skill_verifier.py) AND its outcome
trajectory (agent/outcome_tracker.skill_success_trajectory) clears a success
threshold over enough uses. Skills that fail their test or correlate with
failures get flagged for refinement. This is arXiv 2512.23760's audited
skill-graph idea, grounded in Janus's existing verifier + outcome signal.

Pure + best-effort; the promotability assessment needs no LLM (heuristic over
verifiable signals). Storage: ``~/.janus/learning/skill_graph.json``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_graph_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "skill_graph.json"


def load_graph() -> Dict[str, Any]:
    path = get_graph_path()
    if not path.is_file():
        return {"nodes": {}, "edges": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "nodes" in data and "edges" in data:
            return data
    except (ValueError, OSError):
        pass
    return {"nodes": {}, "edges": []}


def save_graph(data: Dict[str, Any]) -> None:
    path = get_graph_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _skill_names() -> List[str]:
    try:
        from tools.skills_tool import _find_all_skills
        return [s["name"] for s in _find_all_skills()]
    except Exception:
        return []


def build_graph_from_skills(names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Bootstrap/refresh graph nodes from current skills (idempotent).

    New skills get a node at promotion_level 0; nodes for skills that no longer
    exist are sanitized out, and dangling edges with them are dropped.
    """
    if names is None:
        names = _skill_names()
    live = set(names)
    graph = load_graph()
    nodes = graph.get("nodes", {})
    for name in names:
        nodes.setdefault(name, {"promotion_level": 0, "refinement_flagged": False})
    # sanitize: drop nodes + edges referencing skills that no longer exist
    nodes = {n: v for n, v in nodes.items() if n in live}
    edges = [
        e for e in graph.get("edges", [])
        if e.get("from") in live and e.get("to") in live
    ]
    graph = {"nodes": nodes, "edges": edges}
    save_graph(graph)
    return graph


def graph_node_keys() -> List[str]:
    return sorted(load_graph().get("nodes", {}).keys())


def get_node(skill_name: str) -> Optional[Dict[str, Any]]:
    return load_graph().get("nodes", {}).get(skill_name)


def _edge_set(edges: List[Dict[str, str]]) -> Dict[str, set]:
    adj: Dict[str, set] = {}
    for e in edges:
        adj.setdefault(e["from"], set()).add(e["to"])
    return adj


def _creates_cycle(edges: List[Dict[str, str]], frm: str, to: str) -> bool:
    """Would adding frm->to create a cycle? (i.e. is `frm` reachable from `to`?)"""
    adj = _edge_set(edges)
    stack, seen = [to], set()
    while stack:
        node = stack.pop()
        if node == frm:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, ()))
    return False


def add_edge(from_skill: str, to_skill: str, edge_type: str = "prerequisite") -> Tuple[bool, str]:
    """Add ``from_skill -> to_skill``. Refuses self-loops, missing nodes, and cycles."""
    if from_skill == to_skill:
        return False, "self-edges are not allowed"
    graph = load_graph()
    nodes = graph.get("nodes", {})
    if from_skill not in nodes or to_skill not in nodes:
        return False, "both skills must be graph nodes (run build first)"
    edges = graph.get("edges", [])
    if any(e["from"] == from_skill and e["to"] == to_skill for e in edges):
        return True, "edge already exists"
    if _creates_cycle(edges, from_skill, to_skill):
        return False, "would create a cycle"
    edges.append({"from": from_skill, "to": to_skill, "type": edge_type})
    graph["edges"] = edges
    save_graph(graph)
    return True, "added"


def remove_edge(from_skill: str, to_skill: str) -> bool:
    graph = load_graph()
    before = len(graph.get("edges", []))
    graph["edges"] = [
        e for e in graph.get("edges", [])
        if not (e["from"] == from_skill and e["to"] == to_skill)
    ]
    if len(graph["edges"]) != before:
        save_graph(graph)
        return True
    return False


def dependencies_of(skill_name: str) -> List[str]:
    """Transitive prerequisites (everything ``skill_name`` points to)."""
    adj = _edge_set(load_graph().get("edges", []))
    out, stack, seen = [], list(adj.get(skill_name, ())), set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        out.append(node)
        stack.extend(adj.get(node, ()))
    return sorted(out)


def dependents_of(skill_name: str) -> List[str]:
    """Skills that (transitively) depend on ``skill_name``."""
    edges = load_graph().get("edges", [])
    rev = _edge_set([{"from": e["to"], "to": e["from"]} for e in edges])
    out, stack, seen = [], list(rev.get(skill_name, ())), set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        out.append(node)
        stack.extend(rev.get(node, ()))
    return sorted(out)


def cyclic_edges() -> List[Tuple[str, str]]:
    """Return edges that participate in a cycle (should be empty for a healthy DAG)."""
    edges = load_graph().get("edges", [])
    bad = []
    for e in edges:
        others = [x for x in edges if x is not e]
        if _creates_cycle(others, e["from"], e["to"]):
            bad.append((e["from"], e["to"]))
    return bad


def topological_sort() -> List[str]:
    """Kahn's algorithm. Nodes in a cycle are appended at the end (best-effort)."""
    graph = load_graph()
    nodes = list(graph.get("nodes", {}).keys())
    adj = _edge_set(graph.get("edges", []))
    indeg = {n: 0 for n in nodes}
    for src, dsts in adj.items():
        for d in dsts:
            if d in indeg:
                indeg[d] += 1
    queue = sorted([n for n in nodes if indeg[n] == 0])
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for d in sorted(adj.get(n, ())):
            indeg[d] -= 1
            if indeg[d] == 0:
                queue.append(d)
        queue.sort()
    # any nodes left (cycle) appended deterministically
    order.extend(sorted(n for n in nodes if n not in order))
    return order


# --- verifiable-reward promotion --------------------------------------------

def _graph_cfg(key: str, default: Any) -> Any:
    try:
        import yaml
        from janus_constants import get_janus_home
        cfg_path = get_janus_home() / "config.yaml"
        if cfg_path.is_file():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            g = cfg.get("graph", {})
            if isinstance(g, dict) and key in g:
                return g[key]
    except Exception:
        pass
    return default


def _resolve_skill_dir(skill_name: str) -> Optional[Path]:
    try:
        from tools.skills_tool import _find_all_skills
        for s in _find_all_skills():
            if s.get("name") == skill_name and s.get("path"):
                return Path(s["path"])
    except Exception:
        pass
    return None


def assess_promotability(
    skill_name: str,
    *,
    skill_dir: Optional[Path] = None,
    min_uses: Optional[int] = None,
    promo_thr: Optional[float] = None,
    reward_thr: Optional[float] = None,
    trajectory_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Verifiable-reward assessment (no LLM). Combines the static self-test with
    the outcome trajectory. Returns promotable/refinement_needed + the signals.

    ``min_uses`` / ``promo_thr`` / ``reward_thr`` override the ``graph.*`` config
    defaults — the governor passes tightened values under CAUTION so promotion
    bars rise when the learning loop looks shaky, without duplicating this logic.

    Promotion requires BOTH the boolean success rate and the shaped mean reward
    to clear their bars (gap G10). The boolean cannot see a skill that succeeds
    only by thrashing; the reward can.

    ``trajectory_key`` overrides which outcome trajectory is consulted
    (default: ``skill_name``). The shadow-trial lane passes ``draft:<name>``
    so a draft is judged on its OWN record — not on the failing active
    skill's history it is meant to replace."""
    from agent.outcome_tracker import skill_reward_trajectory, skill_success_trajectory

    min_uses = int(_graph_cfg("min_uses_for_promotion", 3)) if min_uses is None else int(min_uses)
    promo_thr = float(_graph_cfg("promotion_success_threshold", 0.75)) if promo_thr is None else float(promo_thr)
    reward_thr = float(_graph_cfg("promotion_reward_threshold", 0.6)) if reward_thr is None else float(reward_thr)
    refine_thr = float(_graph_cfg("refinement_failure_threshold", 0.35))

    if skill_dir is None:
        skill_dir = _resolve_skill_dir(skill_name)
    verify_ok = True
    if skill_dir is not None:
        try:
            from agent.skill_verifier import verify_skill_dir
            verify_ok = bool(verify_skill_dir(skill_dir).get("ok"))
        except Exception:
            verify_ok = True  # absence of a verifier result isn't a failure

    traj = skill_success_trajectory(trajectory_key or skill_name)
    uses = len(traj)
    success_rate = round(sum(traj) / uses, 3) if uses else None

    # Gap G10: the boolean trajectory cannot distinguish "worked" from "worked
    # only after thrashing" — a skill whose every session succeeded on the
    # tenth tool attempt still reads as 100%. The shaped reward
    # (success - 0.5*tool_failure_rate) can, so promotion needs BOTH: the
    # success rate AND a mean-reward floor. Fail-closed, per the promotion-gate
    # asymmetry — advisory reads may fail open, promotion decisions may not.
    rtraj = skill_reward_trajectory(trajectory_key or skill_name)
    mean_reward = round(sum(rtraj) / len(rtraj), 3) if rtraj else None

    promotable = bool(
        verify_ok and uses >= min_uses
        and success_rate is not None and success_rate >= promo_thr
        and mean_reward is not None and mean_reward >= reward_thr
    )
    refinement_needed = bool(
        (not verify_ok)
        or (success_rate is not None and uses >= min_uses and success_rate <= refine_thr)
    )
    if promotable:
        reason = (f"verified + {int(success_rate * 100)}% success over {uses} uses "
                  f"(mean reward {mean_reward})")
    elif (verify_ok and uses >= min_uses and success_rate is not None
          and success_rate >= promo_thr
          and mean_reward is not None and mean_reward < reward_thr):
        reason = (f"{int(success_rate * 100)}% success but mean reward {mean_reward} "
                  f"< {reward_thr} — succeeds only with heavy tool failure")
    elif refinement_needed:
        reason = "failed self-test" if not verify_ok else \
                 f"low success ({int(success_rate * 100)}%) over {uses} uses"
    else:
        reason = "insufficient signal" if uses < min_uses else "stable"
    return {
        "skill": skill_name, "promotable": promotable, "refinement_needed": refinement_needed,
        "verify_ok": verify_ok, "success_rate": success_rate, "uses": uses,
        "mean_reward": mean_reward, "reason": reason,
    }


def promote_skill(skill_name: str) -> Dict[str, Any]:
    graph = load_graph()
    node = graph.get("nodes", {}).get(skill_name)
    if node is None:
        return {"ok": False, "reason": "not a graph node"}
    node["promotion_level"] = min(int(node.get("promotion_level", 0)) + 1, 5)
    node["refinement_flagged"] = False
    save_graph(graph)
    return {"ok": True, "promotion_level": node["promotion_level"]}


def auto_promote_drafts(
    *,
    llm_caller: Optional[Any] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Verifiable, graduated-trust promotion of quarantined draft skills.

    A draft under ``skills/.drafts/`` graduates to the active library ONLY when
    it passes every gate: the governor is not FROZEN, the static self-test
    passes, the outcome trajectory clears the (governor-scaled) success
    threshold over enough uses, and — if the dialectic gate is on — the
    red-team admits it. Anything short of that stays drafted (never deleted).

    Returns a summary dict. Best-effort and never raises into the caller. All
    work is offline (sleep cron) — nothing here touches the live conversation.
    """
    summary: Dict[str, Any] = {
        "checked": 0, "promoted": [], "skipped": [], "blocked_by_governor": False,
    }
    try:
        from agent.self_improvement_governor import admission_allowed, promotion_thresholds

        # Promotion modifies the agent, so the gate fails CLOSED (gap G7): a
        # crashed governor blocks promotion rather than waving it through.
        if not admission_allowed(fail_closed=True):
            summary["blocked_by_governor"] = True
            return summary

        overrides = promotion_thresholds() or {}

        from janus_constants import get_janus_home
        drafts_dir = get_janus_home() / "skills" / ".drafts"
        if not drafts_dir.is_dir():
            return summary

        from agent.skill_utils import parse_frontmatter

        # Active skill names, for red-team "existing" context + collision checks.
        active_names = set()
        try:
            from tools.skills_tool import _find_all_skills
            active_names = {s.get("name") for s in _find_all_skills() if s.get("name")}
        except Exception:
            pass

        candidates = sorted(p for p in drafts_dir.iterdir() if (p / "SKILL.md").is_file())
        if not candidates:
            return summary

        snapshotted = False
        for draft_dir in candidates:
            summary["checked"] += 1
            try:
                meta, _body = parse_frontmatter((draft_dir / "SKILL.md").read_text(encoding="utf-8"))
            except Exception:
                summary["skipped"].append({"skill": draft_dir.name, "reason": "unreadable SKILL.md"})
                continue
            name = (meta.get("name") or draft_dir.name).strip()

            # Shadow-trial lane: when trial_drafts is on, judge the draft on
            # its OWN accumulated draft:-aliased trajectory. Without the flag
            # drafts can't be viewed, so their trajectory is empty and the
            # gate honestly refuses (never promote on zero evidence).
            _trajectory_key = None
            try:
                from agent.feature_flags import flag_enabled
                if flag_enabled("learning", "governor.trial_drafts", default=False):
                    _trajectory_key = f"draft:{draft_dir.name}"
            except Exception:
                _trajectory_key = None

            assessment = assess_promotability(
                name, skill_dir=draft_dir,
                min_uses=overrides.get("min_uses"), promo_thr=overrides.get("promo_thr"),
                reward_thr=overrides.get("reward_thr"),
                trajectory_key=_trajectory_key,
            )
            if not assessment.get("promotable"):
                summary["skipped"].append({"skill": name, "reason": assessment.get("reason", "not promotable")})
                continue

            # Red-team admission gate (fail-open on infra error).
            objection = _red_team_promotion(name, meta, draft_dir, active_names, llm_caller)
            if objection is not None:
                summary["skipped"].append({"skill": name, "reason": f"red-team reject: {objection}"})
                continue

            if dry_run:
                summary["promoted"].append({"skill": name, "reason": assessment.get("reason"), "dry_run": True})
                continue

            # Snapshot once, lazily, before the first real mutation so
            # `janus curator rollback` can undo the whole promotion batch.
            if not snapshotted:
                try:
                    from agent.curator_backup import snapshot_skills
                    snapshot_skills(reason="pre-auto-promote")
                except Exception:
                    logger.debug("pre-promote snapshot failed (continuing)", exc_info=True)
                snapshotted = True

            moved = _activate_draft(draft_dir, name, meta, active_names)
            if moved is None:
                summary["skipped"].append({"skill": name, "reason": "activation move failed"})
                continue
            active_names.add(moved)
            try:
                build_graph_from_skills()
                promote_skill(moved)
            except Exception:
                logger.debug("graph registration after promote failed", exc_info=True)
            summary["promoted"].append({"skill": moved, "reason": assessment.get("reason")})
    except Exception:
        logger.debug("auto_promote_drafts failed", exc_info=True)
    return summary


def _red_team_promotion(name, meta, draft_dir, active_names, llm_caller) -> Optional[str]:
    """Return the skeptic's objection if the red-team REJECTS the skill, else None.

    Fails OPEN: when the gate is off or an infra error occurs, returns None
    (admit), matching skill_miner._red_team_proposals. Mirrors that caller's use
    of the deliberation API exactly (``existing`` is a newline-joined string;
    verdicts carry ``verdict`` in {accept,reject,revise} + ``skeptic_objection``)."""
    try:
        from agent.deliberation import dialectic_enabled, red_team_claims
        if not dialectic_enabled("skills"):
            return None
        content = (draft_dir / "SKILL.md").read_text(encoding="utf-8")[:4000]
        claim = {
            "id": name, "kind": "skill",
            "content": f"Promote skill '{name}': {meta.get('description', '')}\n{content}",
        }
        result = red_team_claims(
            [claim],
            existing="\n".join(sorted(n for n in active_names if n)),
            llm_caller=llm_caller,
        )
        if result.get("error"):
            return None  # infra error → fail open
        verdict = (result.get("verdicts") or {}).get(name) or {}
        if verdict.get("verdict") == "reject":
            return verdict.get("skeptic_objection") or verdict.get("crux") or "rejected"
        return None
    except Exception:
        logger.debug("red-team promotion gate errored — failing open", exc_info=True)
        return None


def _activate_draft(draft_dir: Path, name: str, meta: Dict[str, Any], active_names) -> Optional[str]:
    """Move a draft dir out of .drafts/ into the active tree.

    Same-skill replacement: when an ACTIVE skill already carries this
    frontmatter name, the draft is its improved variant — archive the original
    to ``skills/.archive/`` (never delete) and take over its categorized path.
    Activating beside it as ``<name>-2`` would leave two skills with the same
    frontmatter name, which ``skill_view`` then refuses as ambiguous — the
    "promotion breaks the skill" failure.

    The ``-N`` suffix remains only for genuine different-skill *directory*
    collisions. Returns the activated skill name, or None on failure."""
    try:
        import shutil
        from datetime import datetime, timezone
        from janus_constants import get_janus_home
        from agent.skill_utils import resolve_active_skill_md

        base_root = get_janus_home() / "skills"

        existing_md = resolve_active_skill_md(base_root, name)
        if existing_md is not None:
            existing_dir = existing_md.parent
            archive_root = base_root / ".archive"
            archive_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            archived = archive_root / f"{existing_dir.name}-{stamp}"
            n = 2
            while archived.exists():
                archived = archive_root / f"{existing_dir.name}-{stamp}-{n}"
                n += 1
            shutil.move(str(existing_dir), str(archived))
            shutil.move(str(draft_dir), str(existing_dir))
            return name

        category = ""
        try:
            category = str(((meta.get("metadata") or {}).get("janus") or {}).get("category")
                           or meta.get("category") or "").strip()
        except Exception:
            category = ""
        parent = base_root / category if category else base_root

        target_name = name
        target = parent / target_name
        n = 2
        while target.exists() or target_name in active_names:
            target_name = f"{name}-{n}"
            target = parent / target_name
            n += 1
        parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(draft_dir), str(target))
        return target_name
    except Exception:
        logger.debug("draft activation move failed for %s", name, exc_info=True)
        return None


def flag_refinement_needed(skill_name: str, reason: str = "") -> bool:
    graph = load_graph()
    node = graph.get("nodes", {}).get(skill_name)
    if node is None:
        return False
    node["refinement_flagged"] = True
    if reason:
        node["refinement_reason"] = reason
    save_graph(graph)
    return True


def as_json() -> str:
    return json.dumps(load_graph(), indent=2, ensure_ascii=False)
