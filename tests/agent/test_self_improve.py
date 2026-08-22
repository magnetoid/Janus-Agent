"""Tests for governed self-improvement / DGM-lite (agent/self_improve.py).

The guardrails are the whole point, so they get the most coverage: no core-code
targets, no escapes, and a promotion gate that refuses unless EVERY safety
condition holds. The isolated JANUS_HOME autouse fixture gives each test a clean
archive + artifact tree.
"""
import pytest

from agent import self_improve as si


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    # Feature is off by default; turn it on for the lifecycle tests. Individual
    # tests re-patch to exercise the disabled path.
    monkeypatch.setattr(si, "enabled", lambda config=None: True)
    monkeypatch.setattr(si, "require_human_approval", lambda config=None: True)


@pytest.fixture(autouse=True)
def _satisfied_eval_gate(monkeypatch):
    # can_promote refuses on a small suite (min_eval_specs) and on a
    # history-less trend gate (fail_closed=True). Satisfy both by default so
    # the rest of the matrix stays testable; the eval-gate tests re-patch.
    monkeypatch.setattr("agent.evals.load_eval_specs",
                        lambda *a, **k: [object()] * 10)
    monkeypatch.setattr("agent.eval_trend.regression_gate",
                        lambda *a, **k: {"ok": True, "message": "OK"})


# --- guardrail 1: target allowlist (self-artifacts only, never core) --------

def test_resolve_target_refuses_core_and_escapes():
    assert si.resolve_target("../run_agent.py") is None          # repo/core
    assert si.resolve_target("/etc/passwd") is None              # absolute
    assert si.resolve_target("../../etc/passwd") is None         # escape
    assert si.resolve_target("config.yaml") is None             # janus_home root, not allowlisted
    assert si.resolve_target("memory/USER.md") is None          # other janus dir
    assert si.resolve_target("") is None
    assert si.resolve_target("skills/active/x/SKILL.md") is None  # active skills, not .drafts


def test_resolve_target_accepts_self_artifacts():
    assert si.resolve_target("skills/.drafts/foo/SKILL.md") is not None
    assert si.resolve_target("prompts/frag.txt") is not None
    assert si.resolve_target("policies/curation.yaml") is not None


def test_propose_refuses_bad_target_kind_and_disabled(monkeypatch):
    assert si.propose("skill", "../core.py", "x") is None        # core target
    assert si.propose("core_patch", "skills/.drafts/a/SKILL.md", "x") is None  # bad kind
    monkeypatch.setattr(si, "enabled", lambda config=None: False)
    assert si.propose("skill", "skills/.drafts/a/SKILL.md", "x") is None       # disabled


# --- lifecycle + lineage ----------------------------------------------------

def _propose():
    return si.propose("skill", "skills/.drafts/demo/SKILL.md", "# Demo\nbody",
                      rationale="worth trying")


def test_propose_and_lineage():
    a = _propose()
    assert a["status"] == "proposed" and a["id"] == "si-0001"
    b = si.propose("skill", "skills/.drafts/demo/SKILL.md", "# Demo v2",
                   parent_id=a["id"])
    chain = si.lineage(b["id"])
    assert [c["id"] for c in chain] == [a["id"], b["id"]]


def test_approve_only_from_evaluated():
    a = _propose()
    assert si.approve(a["id"]) is None                # proposed, not evaluated
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.7)
    assert si.approve(a["id"])["status"] == "approved"


def test_record_evaluation_requires_strict_improvement():
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.7)   # improved
    assert si.get(a["id"])["gate_ok"] is True
    b = si.propose("skill", "skills/.drafts/two/SKILL.md", "x")
    si.record_evaluation(b["id"], score_before=0.9, score_after=0.6)   # regressed
    assert si.get(b["id"])["gate_ok"] is False
    # a no-op / fabricated 0→0 (or equal) pair does NOT pass — no measured gain
    c = si.propose("skill", "skills/.drafts/three/SKILL.md", "x")
    si.record_evaluation(c["id"], score_before=0.0, score_after=0.0)
    assert si.get(c["id"])["gate_ok"] is False


def test_cannot_re_evaluate_promoted_proposal():
    # Guards the backup: re-eval→re-promote must not clobber the original.
    target = si.resolve_target("skills/.drafts/keep/SKILL.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ORIGINAL", encoding="utf-8")
    a = si.propose("skill", "skills/.drafts/keep/SKILL.md", "V1")
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.9)
    si.approve(a["id"])
    assert si.promote(a["id"])["promoted"] is True
    # attempt to re-evaluate the promoted proposal → refused
    assert si.record_evaluation(a["id"], score_before=0.9, score_after=0.95) is None
    assert si.get(a["id"])["status"] == "promoted"
    # rollback still restores the true original, not V1
    si.rollback(a["id"])
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


# --- the promotion gate matrix ----------------------------------------------

def test_gate_refuses_until_all_conditions_hold(monkeypatch):
    a = _propose()
    assert si.can_promote(a["id"])[0] is False        # not evaluated
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.8)
    assert si.can_promote(a["id"])[0] is False        # awaiting approval
    si.approve(a["id"])
    ok, reason = si.can_promote(a["id"])
    assert ok is True and reason == "ok"


def test_gate_refuses_when_governor_frozen(monkeypatch):
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.8)
    si.approve(a["id"])
    monkeypatch.setattr("agent.self_improvement_governor.assess_admission_state",
                        lambda *a, **k: {"state": "frozen", "reasons": ["regression"]})
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "FROZEN" in reason


def test_gate_refuses_when_autonomy_blocked(monkeypatch):
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.8)
    si.approve(a["id"])
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason",
                        lambda *a, **k: "rolling 24h spend over cap")
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "safety floor" in reason


def test_gate_refuses_regressed_variant():
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.9, score_after=0.5)  # regressed
    si.approve(a["id"])
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "improvement" in reason


def test_gate_refuses_when_live_suite_regressed(monkeypatch):
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.8)
    si.approve(a["id"])
    monkeypatch.setattr("agent.eval_trend.regression_gate",
                        lambda *a, **k: {"ok": False, "message": "REGRESSION — core_test"})
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "live eval suite gate refused" in reason


def test_gate_fails_closed_when_safety_check_errors(monkeypatch):
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.8)
    si.approve(a["id"])
    def _boom(*a, **k):
        raise RuntimeError("governor import broke")
    monkeypatch.setattr("agent.self_improvement_governor.assess_admission_state", _boom)
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "refusing" in reason        # fail CLOSED, not open


# --- apply / rollback -------------------------------------------------------

def test_promote_activates_net_new_skill_and_rollback_removes_it():
    # A skill promotion must land LIVE — historically it wrote only the
    # quarantined draft, a directory nothing loads.
    from janus_constants import get_janus_home
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.9)
    si.approve(a["id"])
    res = si.promote(a["id"])
    assert res["promoted"] is True
    live = get_janus_home() / "skills" / "demo" / "SKILL.md"
    assert res["target"] == str(live)
    assert live.read_text(encoding="utf-8") == "# Demo\nbody"
    # ...and the draft left quarantine (moved, not copied).
    assert not si.resolve_target(a["target"]).exists()
    # nothing existed before → rollback removes the activated skill
    assert si.rollback(a["id"])["rolled_back"] is True
    assert not live.exists() and not live.parent.exists()


def test_promote_backs_up_and_rollback_restores_prior_content():
    from janus_constants import get_janus_home
    draft = si.resolve_target("skills/.drafts/exists/SKILL.md")
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("ORIGINAL", encoding="utf-8")
    a = si.propose("skill", "skills/.drafts/exists/SKILL.md", "REPLACEMENT")
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.9)
    si.approve(a["id"])
    assert si.promote(a["id"])["promoted"] is True
    live = get_janus_home() / "skills" / "exists" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == "REPLACEMENT"   # went live
    si.rollback(a["id"])
    assert not live.exists()                                    # activation undone
    assert draft.read_text(encoding="utf-8") == "ORIGINAL"      # draft restored


def test_promote_replaces_active_skill_in_place_and_rollback_restores():
    # The Lane-A core case: an ACTIVE (categorized) skill with the same
    # frontmatter name gets replaced in place — no name-2 twin, no ambiguity.
    from janus_constants import get_janus_home
    live_dir = get_janus_home() / "skills" / "devops" / "flaky"
    live_dir.mkdir(parents=True, exist_ok=True)
    live = live_dir / "SKILL.md"
    live.write_text("---\nname: flaky\ndescription: d\n---\n\nOLD BODY",
                    encoding="utf-8")
    variant = "---\nname: flaky\ndescription: d\n---\n\nNEW BODY"
    a = si.propose("skill", "skills/.drafts/flaky/SKILL.md", variant)
    si.record_evaluation(a["id"], score_before=0.2, score_after=0.9)
    si.approve(a["id"])
    res = si.promote(a["id"])
    assert res["promoted"] is True
    assert res["target"] == str(live)
    assert "NEW BODY" in live.read_text(encoding="utf-8")
    # exactly one active skill named 'flaky' — no name-2 twin anywhere
    from agent.skill_utils import iter_skill_index_files, parse_frontmatter
    hits = [md for md in iter_skill_index_files(get_janus_home() / "skills", "SKILL.md")
            if (parse_frontmatter(md.read_text(encoding="utf-8"))[0].get("name")
                or md.parent.name) == "flaky"]
    assert hits == [live]
    # rollback restores the original bytes at the live path
    assert si.rollback(a["id"])["rolled_back"] is True
    assert "OLD BODY" in live.read_text(encoding="utf-8")


def test_promote_refused_does_not_write():
    a = _propose()  # never evaluated/approved
    res = si.promote(a["id"])
    assert res["promoted"] is False
    assert not si.resolve_target(a["target"]).exists()


def test_safe_write_refuses_symlinked_path_component(tmp_path):
    # Second-layer defense: even if a symlink is planted inside the allowlisted
    # tree, the write refuses to traverse it (closes the TOCTOU window).
    from janus_constants import get_janus_home
    root = get_janus_home().resolve()
    drafts = root / "skills" / ".drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (drafts / "evil").symlink_to(outside, target_is_directory=True)
    target = drafts / "evil" / "SKILL.md"
    assert si._has_symlink_component(target, root) is True
    with pytest.raises(OSError):
        si._safe_write(target, b"pwned", root)
    assert not (outside / "SKILL.md").exists()   # nothing written through the link


# --- config reading ---------------------------------------------------------

def test_enabled_and_approval_read_config(monkeypatch):
    monkeypatch.undo()  # drop the autouse patches; test the real config readers
    assert si.enabled({"learning": {"self_improve": {"enabled": True}}}) is True
    assert si.enabled({"learning": {"self_improve": {"enabled": False}}}) is False
    assert si.enabled({}) is False                                   # default off
    assert si.require_human_approval({}) is True                     # default ON
    assert si.require_human_approval(
        {"learning": {"self_improve": {"require_human_approval": False}}}) is False


# --- Phase D: eval-suite floor + fail-closed trend cross-check ---------------

def _approved_proposal():
    a = _propose()
    si.record_evaluation(a["id"], score_before=0.5, score_after=0.9)
    si.approve(a["id"])
    return a


def test_gate_refuses_on_small_or_empty_suite(monkeypatch):
    a = _approved_proposal()
    monkeypatch.setattr("agent.evals.load_eval_specs", lambda *x, **k: [])
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "eval suite too small" in reason
    # a handful of specs below the floor is still refused
    monkeypatch.setattr("agent.evals.load_eval_specs",
                        lambda *x, **k: [object()] * 3)
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "eval suite too small" in reason


def test_gate_refuses_without_trend_history(monkeypatch):
    # regression_gate(fail_closed=True) refusing (no history) must block —
    # the vacuous pass on an empty history was the whole bug.
    a = _approved_proposal()
    monkeypatch.setattr(
        "agent.eval_trend.regression_gate",
        lambda *x, fail_closed=False, **k: {
            "ok": not fail_closed,
            "message": "not enough eval history to compare yet"})
    ok, reason = si.can_promote(a["id"])
    assert ok is False and "gate refused" in reason


def test_gate_passes_fail_closed_flag_to_regression_gate(monkeypatch):
    seen = {}

    def fake_gate(*x, **k):
        seen.update(k)
        return {"ok": True, "message": "OK"}

    a = _approved_proposal()
    monkeypatch.setattr("agent.eval_trend.regression_gate", fake_gate)
    ok, _ = si.can_promote(a["id"])
    assert ok is True
    assert seen.get("fail_closed") is True
