"""Shadow-trial lane for quarantined drafts (learning.governor.trial_drafts).

The load-bearing invariant is the FLAG-OFF case: drafts stay invisible to the
skills index and unloadable by skill_view — the quarantine holds for anyone
who hasn't opted in. Flag ON: drafts surface only under the ``draft:`` alias
(the active skill always owns the bare name), skill_view serves them, and
their outcomes accrue to a ``draft:``-keyed trajectory the promotion gate can
consult.
"""
import json

import pytest

from janus_constants import get_janus_home


@pytest.fixture(autouse=True)
def _repoint_skills_dir(monkeypatch):
    # tools.skills_tool captures SKILLS_DIR/JANUS_HOME at import; repoint them
    # at this test's isolated home (the established test pattern).
    import tools.skills_tool as st
    monkeypatch.setattr(st, "JANUS_HOME", get_janus_home())
    monkeypatch.setattr(st, "SKILLS_DIR", get_janus_home() / "skills")


def _seed_active(name, body="active body"):
    d = get_janus_home() / "skills" / "devops" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: active desc\n---\n\n{body}",
        encoding="utf-8")
    return d


def _seed_draft(name, body="draft body"):
    d = get_janus_home() / "skills" / ".drafts" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: draft desc\n---\n\n{body}",
        encoding="utf-8")
    return d


def _build_prompt():
    from agent import prompt_builder
    prompt_builder._SKILLS_PROMPT_CACHE.clear()
    return prompt_builder.build_skills_system_prompt()


def _flag(monkeypatch, on: bool):
    monkeypatch.setattr(
        "agent.feature_flags.flag_enabled",
        lambda section, key, default=False: (
            on if (section, key) == ("learning", "governor.trial_drafts")
            else default))


# ── Flag OFF: the quarantine holds ──────────────────────────────────────────

def test_flag_off_drafts_invisible_and_unloadable(monkeypatch):
    _flag(monkeypatch, False)
    _seed_active("deploy")
    _seed_draft("deploy-improved")

    prompt = _build_prompt()
    assert "draft:" not in prompt
    assert "deploy-improved" not in prompt

    from tools.skills_tool import skill_view
    out = json.loads(skill_view(name="draft:deploy-improved"))
    assert out["success"] is False


# ── Flag ON: the probation lane ─────────────────────────────────────────────

def test_flag_on_drafts_surface_under_alias_only(monkeypatch):
    _flag(monkeypatch, True)
    _seed_active("deploy")
    _seed_draft("deploy")          # same name as the active skill

    prompt = _build_prompt()
    assert "Draft skills under trial" in prompt
    assert "draft:deploy" in prompt
    # the active skill still owns the bare name inside the main index
    assert "- deploy: active desc" in prompt


def test_flag_on_skill_view_serves_draft_and_bare_name_stays_active(monkeypatch):
    _flag(monkeypatch, True)
    _seed_active("deploy", body="ACTIVE CONTENT")
    _seed_draft("deploy", body="DRAFT CONTENT")

    from tools.skills_tool import skill_view
    draft = json.loads(skill_view(name="draft:deploy"))
    assert draft["success"] is True
    assert "DRAFT CONTENT" in json.dumps(draft)

    active = json.loads(skill_view(name="deploy"))
    assert active["success"] is True
    assert "ACTIVE CONTENT" in json.dumps(active)
    assert "DRAFT CONTENT" not in json.dumps(active)


def test_flag_on_rejects_malformed_draft_names(monkeypatch):
    _flag(monkeypatch, True)
    from tools.skills_tool import skill_view
    out = json.loads(skill_view(name="draft:../escape"))
    assert out["success"] is False


def test_draft_outcomes_accumulate_their_own_trajectory(monkeypatch):
    _flag(monkeypatch, True)
    from agent import outcome_tracker as ot
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "skill_view",
                          "arguments": json.dumps({"name": "draft:deploy"})}}]},
    ]
    used = ot.skills_used_in(messages)
    assert used == ["draft:deploy"]
    # content-rendered form (regex path) also attributes
    rendered = [{"role": "assistant",
                 "content": "skill_view(name='draft:deploy')"}]
    assert ot.skills_used_in(rendered) == ["draft:deploy"]

    for i in range(3):
        ot.record_outcome(f"s{i}", True, skills=["draft:deploy"])
    traj = ot.skill_success_trajectory("draft:deploy")
    assert len(traj) == 3
    # ...and it never bleeds into the active skill's record
    assert ot.skill_success_trajectory("deploy") == []


def test_auto_promote_uses_draft_trajectory_and_replaces_active(monkeypatch):
    """End of the Lane-B chain: a draft with its own good trajectory promotes
    via the same-name replacement path — no name-2 twin, original archived."""
    _flag(monkeypatch, True)
    from agent import outcome_tracker as ot
    from agent import skill_graph as sg

    active_dir = _seed_active("deploy", body="OLD")
    _seed_draft("deploy", body="NEW")
    for i in range(4):
        ot.record_outcome(f"s{i}", True, skills=["draft:deploy"])

    monkeypatch.setattr("agent.self_improvement_governor.admission_allowed",
                        lambda fail_closed=True: True)
    monkeypatch.setattr("agent.self_improvement_governor.promotion_thresholds",
                        lambda: {})

    summary = sg.auto_promote_drafts()

    assert [p["skill"] for p in summary["promoted"]] == ["deploy"]
    assert "NEW" in (active_dir / "SKILL.md").read_text(encoding="utf-8")
    archived = list((get_janus_home() / "skills" / ".archive").glob("deploy-*/SKILL.md"))
    assert len(archived) == 1 and "OLD" in archived[0].read_text(encoding="utf-8")


def test_flag_off_auto_promote_stays_deadlocked(monkeypatch):
    """Honest state without opt-in: no trajectory → no promotion, ever."""
    _flag(monkeypatch, False)
    from agent import outcome_tracker as ot
    from agent import skill_graph as sg

    _seed_draft("newskill", body="NEW")
    # even outcomes recorded under the bare name don't help: without the flag
    # the draft could never have been viewed, so none should exist — and the
    # lane judges drafts on the (empty) bare-name trajectory as before.
    monkeypatch.setattr("agent.self_improvement_governor.admission_allowed",
                        lambda fail_closed=True: True)
    monkeypatch.setattr("agent.self_improvement_governor.promotion_thresholds",
                        lambda: {})
    summary = sg.auto_promote_drafts()
    assert summary["promoted"] == []
