"""End-to-end: the self-improvement loop actually CLOSES.

Every module in the propose → evaluate → approve → promote → rollback chain is
unit-tested; this file tests the COMPOSITION — the historical failure mode was
that each gate passed in isolation while the loop as a whole was a no-op
(promotion wrote into the invisible drafts quarantine, the eval gate passed
vacuously on an empty suite, and the variant arm measured a corrupted home).

Drives the real modules with a deterministic ``agent_runner`` / ``llm_caller``
and a real isolated-JANUS_HOME config.yaml. Asserts the promoted variant is
visible where it matters: in the ACTIVE skill tree and in a freshly built
skills system prompt.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from janus_constants import get_janus_home

OLD_DESC = "flaky helper, old edition"
NEW_DESC = "flaky helper, improved with checklist"
VARIANT = f"---\nname: flaky\ndescription: {NEW_DESC}\n---\n\nAlways use the checklist."


@pytest.fixture()
def loop_home(monkeypatch):
    home = get_janus_home()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "learning": {"self_improve": {
            "enabled": True,
            "require_human_approval": True,
            "min_eval_specs": 3,
            "eval_trials": 1,
            "eval_epsilon": 0.05,
        }},
    }), encoding="utf-8")
    # categorized ACTIVE skill — the real bundled-skill layout
    d = home / "skills" / "devops" / "flaky"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: flaky\ndescription: {OLD_DESC}\n---\n\nOld body, no magic.",
        encoding="utf-8")
    # a small real eval suite: one capability spec keyed to the skill body,
    # two always-passing regression pins
    e = home / "evals"
    e.mkdir(parents=True)
    (e / "suite.yaml").write_text(yaml.safe_dump({"evals": [
        {"name": "uses-checklist", "kind": "capability", "est_minutes": 5,
         "prompt": "do the flaky task",
         "checks": [{"type": "contains", "value": "checklist"}]},
        {"name": "stays-polite", "kind": "regression",
         "prompt": "say ok",
         "checks": [{"type": "contains", "value": "OK"}]},
        {"name": "stays-brief", "kind": "regression",
         "prompt": "be brief",
         "checks": [{"type": "max_length", "value": 200}]},
    ]}), encoding="utf-8")
    # the failing skill is agent-created (proposer only rewrites its own work)
    monkeypatch.setattr("tools.skill_usage.is_agent_created", lambda name: True)
    return home


def _runner(spec):
    """Deterministic stand-in for run_conversation: 'uses-checklist' passes
    iff the ACTIVE flaky skill body mentions a checklist; the regression pins
    always pass."""
    from agent.skill_utils import resolve_active_skill_md
    if spec.name == "uses-checklist":
        md = resolve_active_skill_md(get_janus_home() / "skills", "flaky")
        body = md.read_text(encoding="utf-8") if md else ""
        resp = "checklist applied" if "checklist" in body else "no help"
        return {"final_response": resp, "messages": []}
    return {"final_response": "OK", "messages": []}


def _llm(reply):
    def caller(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    return caller


def _fresh_skills_prompt():
    from agent import prompt_builder
    prompt_builder._SKILLS_PROMPT_CACHE.clear()
    snap = get_janus_home() / "skills" / ".skills_prompt_snapshot.json"
    if snap.exists():
        snap.unlink()
    return prompt_builder.build_skills_system_prompt()


def test_full_loop_closes_and_rolls_back(loop_home):
    from agent import eval_orchestrator, eval_trend, proposer, self_improve

    # ── trend history: two same-suite_hash points so the fail-closed gate
    #    has something to compare ─────────────────────────────────────────
    for _ in range(2):
        rec = eval_trend.run_trend(agent_runner=_runner, trials=1)
        assert "error" not in rec

    # ── PROPOSE: the failing skill earns an improvement proposal ────────
    from agent import outcome_tracker as ot
    for i in range(4):
        ot.record_outcome(f"s{i}", False, skills=["flaky"])
    out = proposer.propose_skill_improvements(llm_caller=_llm(VARIANT),
                                              max_proposals=1)
    assert len(out["proposed"]) == 1, out
    pid = out["proposed"][0]

    # ── EVALUATE: variant beats baseline on the real (isolated) suite ───
    ev = eval_orchestrator.evaluate_proposal(pid, agent_runner=_runner)
    assert ev["evaluated"] is True, ev
    assert ev["score_after"] > ev["score_before"]
    assert self_improve.get(pid)["gate_ok"] is True

    # ── APPROVE + PROMOTE: the change lands LIVE, in place ──────────────
    assert self_improve.approve(pid, by="human")["status"] == "approved"
    ok, reason = self_improve.can_promote(pid)
    assert ok is True, reason
    res = self_improve.promote(pid)
    assert res["promoted"] is True, res

    live = loop_home / "skills" / "devops" / "flaky" / "SKILL.md"
    assert "checklist" in live.read_text(encoding="utf-8")
    assert not (loop_home / "skills" / "flaky-2").exists()
    assert not (loop_home / "skills" / "devops" / "flaky-2").exists()

    # ...and the change is visible in a freshly built skills prompt.
    prompt = _fresh_skills_prompt()
    assert NEW_DESC in prompt
    assert OLD_DESC not in prompt

    # ── ROLLBACK: original bytes and original prompt restored ───────────
    assert self_improve.rollback(pid)["rolled_back"] is True
    assert OLD_DESC in live.read_text(encoding="utf-8")
    prompt = _fresh_skills_prompt()
    assert OLD_DESC in prompt and NEW_DESC not in prompt


def test_regressing_variant_is_refused_end_to_end(loop_home):
    """Negative arm: a variant that flips a passing regression pin must fail
    the eval gate, and promote() must refuse it."""
    from agent import eval_orchestrator, eval_trend, proposer, self_improve

    for _ in range(2):
        eval_trend.run_trend(agent_runner=_runner, trials=1)

    from agent import outcome_tracker as ot
    for i in range(4):
        ot.record_outcome(f"s{i}", False, skills=["flaky"])
    bad_variant = ("---\nname: flaky\ndescription: worse\n---\n\n"
                   "checklist, but rude.")
    out = proposer.propose_skill_improvements(llm_caller=_llm(bad_variant),
                                              max_proposals=1)
    pid = out["proposed"][0]

    def bad_runner(spec):
        # With the bad variant live, the 'stays-polite' regression pin fails.
        from agent.skill_utils import resolve_active_skill_md
        md = resolve_active_skill_md(get_janus_home() / "skills", "flaky")
        body = md.read_text(encoding="utf-8") if md else ""
        if spec.name == "stays-polite" and "rude" in body:
            return {"final_response": "whatever", "messages": []}
        return _runner(spec)

    ev = eval_orchestrator.evaluate_proposal(pid, agent_runner=bad_runner)
    assert ev["evaluated"] is True
    assert self_improve.get(pid)["gate_ok"] is False
    assert "stays-polite" in ev["detail"]["regression_flips"]

    # even a (mistaken) human approval can't push it through the gate
    self_improve.approve(pid, by="human")
    ok, reason = self_improve.can_promote(pid)
    assert ok is False and "improvement" in reason
    assert self_improve.promote(pid)["promoted"] is False
    live = loop_home / "skills" / "devops" / "flaky" / "SKILL.md"
    assert "rude" not in live.read_text(encoding="utf-8")
