"""Invariants for verifiable, graduated-trust draft promotion.

Exercises the ORCHESTRATION gates in skill_graph.auto_promote_drafts: the
governor freeze, the verifiable-promotability gate, the red-team gate, the
non-clobbering archive-not-delete move, and dry-run. assess_promotability and
the deliberation API are stubbed so each gate can be isolated.
"""

from __future__ import annotations

import pytest

from agent import skill_graph as sg
from agent import self_improvement_governor as gov


def _mk_draft(home, name, category="cat", description="test skill"):
    d = home / "skills" / ".drafts" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  janus:\n    category: {category}\n---\nbody\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    import janus_constants
    monkeypatch.setattr(janus_constants, "get_janus_home", lambda: tmp_path)
    # Isolate side effects: no real tar snapshot, no graph file writes.
    import agent.curator_backup as cb
    monkeypatch.setattr(cb, "snapshot_skills", lambda reason="manual": None)
    monkeypatch.setattr(sg, "build_graph_from_skills", lambda names=None: {})
    monkeypatch.setattr(sg, "promote_skill", lambda n: {"ok": True})
    # Default: governor permits, OK thresholds. Tests override as needed.
    # ``**kw`` absorbs the fail_closed kwarg the promotion gate now passes (G7).
    monkeypatch.setattr(gov, "admission_allowed", lambda metrics=None, **kw: True)
    monkeypatch.setattr(gov, "promotion_thresholds", lambda metrics=None, **kw: {})
    return tmp_path


def _promotable(*, ok=True):
    return lambda name, **kw: {
        "skill": name, "promotable": ok, "refinement_needed": not ok,
        "verify_ok": ok, "success_rate": 0.9 if ok else 0.2, "uses": 5,
        "reason": "verified" if ok else "low success",
    }


# ── governor freeze ──────────────────────────────────────────────────────


def test_frozen_blocks_all_promotion(home, monkeypatch):
    draft = _mk_draft(home, "alpha")
    monkeypatch.setattr(gov, "admission_allowed", lambda metrics=None, **kw: False)
    monkeypatch.setattr(sg, "assess_promotability", _promotable(ok=True))

    out = sg.auto_promote_drafts()
    assert out["blocked_by_governor"] is True
    assert out["promoted"] == []
    assert draft.is_dir()  # untouched


# ── happy path ───────────────────────────────────────────────────────────


def test_promotes_when_all_gates_pass(home, monkeypatch):
    draft = _mk_draft(home, "beta", category="devops")
    monkeypatch.setattr(sg, "assess_promotability", _promotable(ok=True))

    out = sg.auto_promote_drafts()
    assert [p["skill"] for p in out["promoted"]] == ["beta"]
    assert not draft.is_dir()  # moved out of .drafts/
    assert (home / "skills" / "devops" / "beta" / "SKILL.md").is_file()


def test_not_promotable_kept_drafted(home, monkeypatch):
    draft = _mk_draft(home, "gamma")
    monkeypatch.setattr(sg, "assess_promotability", _promotable(ok=False))

    out = sg.auto_promote_drafts()
    assert out["promoted"] == []
    assert {s["skill"] for s in out["skipped"]} == {"gamma"}
    assert draft.is_dir()  # still drafted


# ── red-team gate ────────────────────────────────────────────────────────


def test_red_team_reject_blocks_promotion(home, monkeypatch):
    draft = _mk_draft(home, "delta")
    monkeypatch.setattr(sg, "assess_promotability", _promotable(ok=True))

    import agent.deliberation as delib
    monkeypatch.setattr(delib, "dialectic_enabled", lambda path: True)
    monkeypatch.setattr(delib, "red_team_claims", lambda claims, **kw: {
        "verdicts": {"delta": {"verdict": "reject", "skeptic_objection": "duplicate"}},
        "error": None,
    })

    out = sg.auto_promote_drafts()
    assert out["promoted"] == []
    assert any("red-team" in s["reason"] for s in out["skipped"])
    assert draft.is_dir()


def test_red_team_infra_error_fails_open(home, monkeypatch):
    _mk_draft(home, "epsilon")
    monkeypatch.setattr(sg, "assess_promotability", _promotable(ok=True))

    import agent.deliberation as delib
    monkeypatch.setattr(delib, "dialectic_enabled", lambda path: True)
    monkeypatch.setattr(delib, "red_team_claims", lambda claims, **kw: {
        "verdicts": {}, "error": "arbiter unreachable",
    })

    out = sg.auto_promote_drafts()
    # infra error → fail open → still promoted
    assert [p["skill"] for p in out["promoted"]] == ["epsilon"]


# ── archive-not-delete on name collision ─────────────────────────────────


def test_same_name_draft_archives_the_original_and_takes_over(home, monkeypatch):
    """A same-name draft is the skill's improved variant: it takes over the
    active path, and the original is ARCHIVED rather than deleted.

    This previously asserted the opposite mechanism (original left in place,
    draft promoted under a ``-N`` suffix). ``_activate_draft`` now reserves the
    suffix for genuine different-skill directory collisions. The load-bearing
    property is unchanged and is what this pins: promotion must never destroy
    a user's existing skill.
    """
    active = home / "skills" / "cat" / "zeta"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("---\nname: zeta\n---\nORIGINAL\n", encoding="utf-8")
    _mk_draft(home, "zeta", category="cat")
    monkeypatch.setattr(sg, "assess_promotability", _promotable(ok=True))

    out = sg.auto_promote_drafts()

    # The draft took over the canonical path under its own name.
    assert out["promoted"] and out["promoted"][0]["skill"] == "zeta"
    assert active.is_dir()
    assert "ORIGINAL" not in (active / "SKILL.md").read_text(encoding="utf-8")

    # Never delete: the original content still exists under skills/.archive/.
    archived = list((home / "skills" / ".archive").glob("zeta-*/SKILL.md"))
    assert len(archived) == 1, f"original not archived: {archived}"
    assert archived[0].read_text(encoding="utf-8").strip().endswith("ORIGINAL")


# ── dry run ──────────────────────────────────────────────────────────────


def test_dry_run_reports_without_moving(home, monkeypatch):
    draft = _mk_draft(home, "eta")
    monkeypatch.setattr(sg, "assess_promotability", _promotable(ok=True))

    out = sg.auto_promote_drafts(dry_run=True)
    assert out["promoted"] and out["promoted"][0].get("dry_run") is True
    assert draft.is_dir()  # nothing moved


# ── CAUTION tightening is threaded into the assessment ───────────────────


def test_caution_thresholds_passed_to_assessment(home, monkeypatch):
    _mk_draft(home, "theta")
    monkeypatch.setattr(gov, "promotion_thresholds",
                        lambda metrics=None: {"min_uses": 5, "promo_thr": 0.85})
    seen = {}

    # ``**kw`` absorbs kwargs the gate grows over time (e.g. trajectory_key from
    # the shadow-trial lane). Without it the call raises TypeError, which
    # auto_promote_drafts swallows by contract — the spy is simply never
    # reached and the assertion below fails against an empty dict.
    def spy(name, *, skill_dir=None, min_uses=None, promo_thr=None, **kw):
        seen["min_uses"] = min_uses
        seen["promo_thr"] = promo_thr
        return {"promotable": False, "reason": "stub"}

    monkeypatch.setattr(sg, "assess_promotability", spy)
    sg.auto_promote_drafts()
    assert seen == {"min_uses": 5, "promo_thr": 0.85}
