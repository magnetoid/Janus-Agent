"""Eval orchestrator (Phase 2.1b) — makes a proposal promotable.

The proposer generates skill-variant proposals but leaves them without eval
evidence, so ``can_promote`` refuses them. This orchestrator runs the move-2
eval suite twice — once against the current skill (baseline), once with the
variant applied in an ISOLATED throwaway home (never the live profile) — and
records the comparison via ``self_improve.record_evaluation``. A variant that
measurably helps then clears the eval half of the quadruple gate; a human still
approves and promotes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import eval_orchestrator, self_improve


def _write_skill(home: Path, name: str, body: str):
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8")


def _write_eval(home: Path):
    e = home / "evals"
    e.mkdir(parents=True, exist_ok=True)
    (e / "s.yaml").write_text(
        "name: uses-checklist\nprompt: do the flaky task\n"
        "checks:\n  - type: contains\n    value: checklist\n", encoding="utf-8")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / ".janus"
    (h / "skills").mkdir(parents=True)
    monkeypatch.setenv("JANUS_HOME", str(h))
    _write_skill(h, "flaky", "old body without the magic word")
    _write_eval(h)
    monkeypatch.setattr(self_improve, "enabled", lambda config=None: True)
    return h


def _runner_reads_active_skill(spec):
    """Test agent_runner: 'passes' the eval iff the ACTIVE flaky skill body
    contains 'checklist' — so it distinguishes the baseline home from the
    variant home purely by what skill content is live in each isolated run."""
    from janus_constants import get_janus_home
    body = (get_janus_home() / "skills" / "flaky" / "SKILL.md").read_text(encoding="utf-8")
    resp = "checklist applied" if "checklist" in body else "no help"
    return {"final_response": resp, "messages": []}


def test_evaluate_records_improvement_for_a_better_variant(home, monkeypatch):
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    variant = "---\nname: flaky\ndescription: d\n---\n\nNow with a checklist step."
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)

    out = eval_orchestrator.evaluate_proposal(
        rec["id"], agent_runner=_runner_reads_active_skill)

    assert out["evaluated"] is True
    assert out["score_before"] == 0.0   # baseline skill lacks 'checklist'
    assert out["score_after"] == 1.0    # variant adds it
    updated = self_improve.get(rec["id"])
    assert updated["status"] == "evaluated"
    assert updated["gate_ok"] is True   # strict improvement


def test_live_profile_is_never_mutated(home, monkeypatch):
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    before = (home / "skills" / "flaky" / "SKILL.md").read_text(encoding="utf-8")
    variant = "---\nname: flaky\ndescription: d\n---\n\nNow with a checklist step."
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)
    eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=_runner_reads_active_skill)
    after = (home / "skills" / "flaky" / "SKILL.md").read_text(encoding="utf-8")
    assert after == before   # the variant was applied only in a throwaway home


def test_no_eval_specs_is_a_clean_noop(home, monkeypatch):
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    import shutil
    shutil.rmtree(home / "evals")   # no suite to evaluate against
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md",
                               "---\nname: flaky\ndescription: d\n---\n\nx")
    out = eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=_runner_reads_active_skill)
    assert out["evaluated"] is False
    assert "no eval" in out["reason"].lower()
    assert self_improve.get(rec["id"])["status"] == "proposed"  # unchanged


def test_gated_off_when_self_improve_disabled(home, monkeypatch):
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md",
                               "---\nname: flaky\ndescription: d\n---\n\nx")
    monkeypatch.setattr(self_improve, "enabled", lambda config=None: False)
    out = eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=_runner_reads_active_skill)
    assert out["evaluated"] is False
    assert "disabled" in out["reason"]


def test_shaped_reward_lets_a_cleaner_variant_clear_the_gate(home, monkeypatch):
    """Phase 2.3: promotion consumes the shaped reward. Baseline and variant pass
    the SAME eval check, so a pass-rate-only gate would call it a no-op (blocked).
    But the variant reaches the pass with a clean tool trajectory while the
    baseline thrashes — the shaped score rises, so the gate opens."""
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    variant = "---\nname: flaky\ndescription: d\n---\n\nchecklist, done cleanly."

    def runner(spec):
        # Both bodies contain 'checklist' → both PASS the eval. The baseline body
        # (no 'cleanly') drives a failed tool call; the variant runs clean.
        from janus_constants import get_janus_home
        body = (get_janus_home() / "skills" / "flaky" / "SKILL.md").read_text(encoding="utf-8")
        thrash = "cleanly" not in body
        msgs = ([{"role": "tool", "content": '{"error": "x"}'}] if thrash
                else [{"role": "tool", "content": "ok"}])
        return {"final_response": "checklist applied", "messages": msgs}

    # baseline skill also passes the check, so pass-rate is identical (1.0 vs 1.0)
    _write_skill(home, "flaky", "checklist but messy")
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)
    out = eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=runner)

    assert out["evaluated"] is True
    # baseline: pass with tfr=1.0 → shaped 0.5 ; variant: pass clean → shaped 1.0
    assert out["score_before"] == 0.5
    assert out["score_after"] == 1.0
    assert self_improve.get(rec["id"])["gate_ok"] is True   # shaped improvement


def test_worse_variant_records_no_improvement(home, monkeypatch):
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    # baseline already has the magic word; the variant removes it → regression.
    _write_skill(home, "flaky", "the checklist is already here")
    variant = "---\nname: flaky\ndescription: d\n---\n\nremoved the good part."
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)
    out = eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=_runner_reads_active_skill)
    assert out["score_before"] == 1.0
    assert out["score_after"] == 0.0
    assert self_improve.get(rec["id"])["gate_ok"] is False   # not an improvement


# ── Categorized layout (the real bundled-skill layout) ──────────────────────
# Bundled skills live at skills/<category>/<name>/SKILL.md. The variant used
# to be written to a flat skills/<name>/ beside the surviving categorized
# copy — two SKILL.md files with the same frontmatter name, an install
# skill_view refuses to load. These tests pin the in-place overwrite fix.

def _write_categorized_skill(home: Path, category: str, name: str, body: str):
    d = home / "skills" / category / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8")


def _runner_resolves_by_name(spec):
    """Pass iff exactly ONE active skill named 'flaky' exists AND its body
    contains 'checklist' — a duplicate-name (corrupted) home always fails."""
    from janus_constants import get_janus_home
    from agent.skill_utils import iter_skill_index_files, parse_frontmatter
    hits = []
    for md in iter_skill_index_files(get_janus_home() / "skills", "SKILL.md"):
        fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        if (fm.get("name") or md.parent.name) == "flaky":
            hits.append(md)
    if len(hits) != 1:
        return {"final_response": "corrupt: duplicate or missing skill",
                "messages": []}
    body = hits[0].read_text(encoding="utf-8")
    resp = "checklist applied" if "checklist" in body else "no help"
    return {"final_response": resp, "messages": []}


@pytest.fixture()
def categorized_home(tmp_path, monkeypatch):
    h = tmp_path / ".janus"
    (h / "skills").mkdir(parents=True)
    monkeypatch.setenv("JANUS_HOME", str(h))
    _write_categorized_skill(h, "devops", "flaky", "old body without the magic word")
    _write_eval(h)
    monkeypatch.setattr(self_improve, "enabled", lambda config=None: True)
    return h


def test_variant_overwrites_categorized_skill_in_place(categorized_home, monkeypatch):
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    variant = "---\nname: flaky\ndescription: d\n---\n\nNow with a checklist step."
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)

    out = eval_orchestrator.evaluate_proposal(
        rec["id"], agent_runner=_runner_resolves_by_name)

    assert out["evaluated"] is True
    assert out["score_before"] == 0.0   # baseline body lacks 'checklist'
    # A genuinely better variant scores higher — and the runner fails any home
    # containing duplicate 'flaky' skills, so this also proves the variant was
    # applied in place rather than beside the categorized original.
    assert out["score_after"] == 1.0
    # And the live categorized skill was never touched.
    live = (categorized_home / "skills" / "devops" / "flaky" / "SKILL.md")
    assert "old body" in live.read_text(encoding="utf-8")


def test_net_new_skill_variant_gets_a_fresh_directory(categorized_home, monkeypatch):
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)

    def runner(spec):
        from janus_constants import get_janus_home
        md = get_janus_home() / "skills" / "brand-new" / "SKILL.md"
        ok = md.is_file() and "checklist" in md.read_text(encoding="utf-8")
        return {"final_response": "checklist applied" if ok else "no help",
                "messages": []}

    variant = "---\nname: brand-new\ndescription: d\n---\n\nchecklist inside."
    rec = self_improve.propose("skill", "skills/.drafts/brand-new/SKILL.md", variant)
    out = eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=runner)
    assert out["evaluated"] is True
    assert out["score_after"] == 1.0    # net-new variant landed at skills/<name>/
    assert not (categorized_home / "skills" / "brand-new").exists()  # live untouched


# ── Noise-aware gate (_compute_gate) ────────────────────────────────────────

def _arm(score, rewards, passes, kinds):
    return {"score": score, "per_eval_reward": rewards,
            "per_eval_pass": passes, "kinds": kinds}


def test_regression_flip_always_vetoes_even_with_higher_mean():
    before = _arm(0.5, {"reg": 1.0, "cap": 0.0},
                  {"reg": 1.0, "cap": 0.0},
                  {"reg": "regression", "cap": "capability"})
    after = _arm(0.6, {"reg": 0.0, "cap": 1.0},   # mean up, regression down
                 {"reg": 0.0, "cap": 1.0},
                 {"reg": "regression", "cap": "capability"})
    ok, detail = eval_orchestrator._compute_gate(before, after, 0.05)
    assert ok is False
    assert detail["regression_flips"] == ["reg"]


def test_equal_within_epsilon_plus_capability_gain_passes():
    kinds = {"reg": "regression", "cap": "capability"}
    before = _arm(0.5, {"reg": 1.0, "cap": 0.0}, {"reg": 1.0, "cap": 0.0}, kinds)
    # capability improves fully; aggregate dips 0.03 < epsilon
    after = _arm(0.47, {"reg": 0.9, "cap": 1.0}, {"reg": 1.0, "cap": 1.0}, kinds)
    ok, detail = eval_orchestrator._compute_gate(before, after, 0.05)
    assert ok is True
    assert "cap" in detail["improved_evals"]


def test_identical_arms_fail_the_gate():
    kinds = {"a": "regression"}
    arm = _arm(1.0, {"a": 1.0}, {"a": 1.0}, kinds)
    ok, detail = eval_orchestrator._compute_gate(arm, dict(arm), 0.05)
    assert ok is False
    assert detail["improved_evals"] == []


def test_zero_to_zero_fabricated_pair_fails():
    kinds = {"a": "regression"}
    arm = _arm(0.0, {"a": 0.0}, {"a": 0.0}, kinds)
    ok, _ = eval_orchestrator._compute_gate(arm, dict(arm), 0.05)
    assert ok is False


def test_multi_trial_stochastic_runner_does_not_flip_gate(home, monkeypatch):
    """A runner that passes only on odd invocations must not fool the gate in
    either direction: identical stochastic behavior in both arms is a no-op."""
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    calls = {"n": 0}

    def flaky_runner(spec):
        calls["n"] += 1
        resp = "checklist applied" if calls["n"] % 2 else "no help"
        return {"final_response": resp, "messages": []}

    variant = "---\nname: flaky\ndescription: d\n---\n\nsame as ever."
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)
    out = eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=flaky_runner,
                                              config={"learning": {"self_improve": {
                                                  "enabled": True, "eval_trials": 2}}})
    assert out["evaluated"] is True
    # Both arms sampled the same alternating distribution → no >=0.5 per-eval
    # gain is available, so the gate must refuse.
    assert self_improve.get(rec["id"])["gate_ok"] is False


def test_eval_detail_is_recorded_on_the_proposal(home, monkeypatch):
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    variant = "---\nname: flaky\ndescription: d\n---\n\nNow with a checklist step."
    rec = self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)
    eval_orchestrator.evaluate_proposal(rec["id"], agent_runner=_runner_reads_active_skill)
    updated = self_improve.get(rec["id"])
    detail = updated.get("eval_detail")
    assert isinstance(detail, dict)
    assert detail.get("trials", 0) >= 1
    assert "uses-checklist" in detail.get("improved_evals", [])
