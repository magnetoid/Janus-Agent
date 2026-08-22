"""Twin-core adversarial review (Phase 2.2) — the second head, veto-only.

The proposer core generates a skill-variant proposal; a SEPARATE reviewer core
red-teams it before a human ever sees it. The load-bearing invariant is the
asymmetry the twin-core design mandates: the reviewer can only ever VETO a
proposal (reject it with an objection) — it can NEVER approve one or advance it
toward promotion. Mutual LLM approval is banned (collusion / correlated blind
spots / poison propagation); a human still approves everything. These tests pin
that asymmetry, plus fail-open-on-infra-error and the off-by-default gate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import self_improve, twin_review


# --- a fake auxiliary LLM that drives deliberation.red_team_claims -----------

class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


def _caller(pid, arbiter_verdict, *, objection="removes a guardrail"):
    """red_team_claims calls advocate → skeptic → arbiter. Only the arbiter's
    JSON array matters; echo the claim id (== proposal id) with the verdict."""
    def call(task, messages, **kw):
        if task == "dialectic_arbiter":
            # A real "revise" carries revised text (else red_team_claims
            # normalizes it to accept); reject/accept do not.
            revised = "revised skill text" if arbiter_verdict == "revise" else None
            return _Resp(json.dumps([{
                "id": pid, "verdict": arbiter_verdict, "confidence": "high",
                "revised_content": revised, "crux": "load-bearing concern",
                "skeptic_objection": objection,
            }]))
        return _Resp("an argument")
    return call


def _broken_caller(*_a, **_k):
    def call(task, messages, **kw):
        if task == "dialectic_arbiter":
            return _Resp("not json at all")   # arbiter unparseable → fail open
        return _Resp("an argument")
    return call


CFG = {"learning": {"self_improve": {
    "enabled": True, "twin_review": True, "require_human_approval": True}}}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / ".janus"
    (h / "skills" / "flaky").mkdir(parents=True)
    (h / "skills" / "flaky" / "SKILL.md").write_text(
        "---\nname: flaky\ndescription: d\n---\n\ncurrent body", encoding="utf-8")
    monkeypatch.setenv("JANUS_HOME", str(h))
    monkeypatch.setattr(self_improve, "enabled", lambda config=None: True)
    monkeypatch.setattr("agent.self_improvement_governor.learning_frozen", lambda: False)
    monkeypatch.setattr("agent.autonomy_guard.blocked_reason", lambda config=None: None)
    return h


def _propose(home):
    variant = "---\nname: flaky\ndescription: d\n---\n\nrevised body"
    return self_improve.propose("skill", "skills/.drafts/flaky/SKILL.md", variant,
                                config=CFG)


def test_veto_rejects_the_proposal(home):
    rec = _propose(home)
    out = twin_review.review_proposal(
        rec["id"], llm_caller=_caller(rec["id"], "reject"), config=CFG)
    assert out["reviewed"] is True
    assert out["verdict"] == "veto"
    assert "guardrail" in out["objection"]
    updated = self_improve.get(rec["id"])
    assert updated["status"] == "rejected"


def test_revise_is_also_a_veto(home):
    # A "revise" verdict means the objection is real but fixable — for a
    # self-modification gate that is still a real flaw, so it blocks.
    rec = _propose(home)
    out = twin_review.review_proposal(
        rec["id"], llm_caller=_caller(rec["id"], "revise"), config=CFG)
    assert out["verdict"] == "veto"
    assert self_improve.get(rec["id"])["status"] == "rejected"


def test_pass_never_approves_and_never_promotes(home, monkeypatch):
    """The whole point: a passing review does NOT approve or set gate_ok. It only
    records that the second core didn't object; a human still gates promotion."""
    rec = _propose(home)
    # Give it eval evidence so the ONLY thing between it and promotion is approval.
    self_improve.record_evaluation(rec["id"], score_before=0.0, score_after=1.0)
    # ...and satisfy the suite-floor + trend-history preconditions, which
    # otherwise refuse first (they're covered in test_self_improve).
    monkeypatch.setattr("agent.evals.load_eval_specs", lambda *a, **k: [object()] * 10)
    monkeypatch.setattr("agent.eval_trend.regression_gate",
                        lambda *a, **k: {"ok": True, "message": "OK"})
    out = twin_review.review_proposal(
        rec["id"], llm_caller=_caller(rec["id"], "accept"), config=CFG)
    assert out["reviewed"] is True
    assert out["verdict"] == "pass"
    updated = self_improve.get(rec["id"])
    assert updated["status"] == "evaluated"      # NOT advanced to approved
    assert updated["approved_by"] == ""          # the reviewer cannot sign off
    assert updated["gate_ok"] is True            # eval result untouched
    # And promotion is still blocked purely on the human gate.
    ok, reason = self_improve.can_promote(rec["id"], CFG)
    assert ok is False
    assert "approval" in reason


def test_infra_error_fails_open_without_vetoing(home):
    rec = _propose(home)
    out = twin_review.review_proposal(
        rec["id"], llm_caller=_broken_caller(), config=CFG)
    assert out["reviewed"] is False
    assert self_improve.get(rec["id"])["status"] == "proposed"  # not rejected


def test_disabled_is_a_clean_noop(home):
    rec = _propose(home)
    cfg = {"learning": {"self_improve": {"enabled": True, "twin_review": False}}}
    out = twin_review.review_proposal(
        rec["id"], llm_caller=_caller(rec["id"], "reject"), config=cfg)
    assert out["reviewed"] is False
    assert "disabled" in out["reason"]
    assert self_improve.get(rec["id"])["status"] == "proposed"


def test_record_review_cannot_change_status_or_approve(home):
    """Data-layer guarantee: record_review only annotates; it can never move a
    proposal toward promotion, whatever a caller passes."""
    rec = _propose(home)
    self_improve.record_review(rec["id"], verdict="pass", objection="")
    updated = self_improve.get(rec["id"])
    assert updated["status"] == "proposed"       # unchanged
    assert updated["gate_ok"] is False           # unchanged
    assert updated["approved_by"] == ""          # unchanged
    assert updated["review"]["verdict"] == "pass"


def test_only_reviewable_states_are_touched(home):
    rec = _propose(home)
    self_improve.reject(rec["id"], reason="already dead")
    out = twin_review.review_proposal(
        rec["id"], llm_caller=_caller(rec["id"], "reject"), config=CFG)
    assert out["reviewed"] is False
    assert "state" in out["reason"].lower()
