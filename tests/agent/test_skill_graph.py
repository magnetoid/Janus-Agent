"""Tests for the skill graph + verifiable-reward promotion (agent/skill_graph.py)."""
import pytest

from agent import skill_graph as sg
from agent import outcome_tracker as ot


def _build(monkeypatch, names):
    monkeypatch.setattr(sg, "_skill_names", lambda: names)
    return sg.build_graph_from_skills()


def test_build_graph_from_skills_bootstraps_nodes(monkeypatch):
    _build(monkeypatch, ["a", "b", "c"])
    assert sg.graph_node_keys() == ["a", "b", "c"]
    assert sg.get_node("a") == {"promotion_level": 0, "refinement_flagged": False}


def test_build_sanitizes_dropped_skills_and_edges(monkeypatch):
    _build(monkeypatch, ["a", "b"])
    assert sg.add_edge("a", "b") == (True, "added")
    # 'b' disappears -> node + its edge sanitized out on rebuild
    _build(monkeypatch, ["a"])
    assert sg.graph_node_keys() == ["a"]
    assert sg.load_graph()["edges"] == []


def test_add_edge_rejects_self_missing_and_cycles(monkeypatch):
    _build(monkeypatch, ["a", "b", "c"])
    assert sg.add_edge("a", "a")[0] is False           # self
    assert sg.add_edge("a", "ghost")[0] is False        # missing node
    assert sg.add_edge("a", "b")[0] is True
    assert sg.add_edge("b", "c")[0] is True
    assert sg.add_edge("c", "a") == (False, "would create a cycle")  # a->b->c->a
    assert sg.cyclic_edges() == []                       # graph stayed acyclic


def test_dependencies_and_dependents(monkeypatch):
    _build(monkeypatch, ["a", "b", "c"])
    sg.add_edge("a", "b"); sg.add_edge("b", "c")
    assert sg.dependencies_of("a") == ["b", "c"]   # transitive prerequisites
    assert sg.dependents_of("c") == ["a", "b"]      # transitive dependents


def test_topological_sort(monkeypatch):
    _build(monkeypatch, ["a", "b", "c"])
    sg.add_edge("a", "b"); sg.add_edge("b", "c")
    order = sg.topological_sort()
    assert order.index("a") < order.index("b") < order.index("c")


def test_assess_promotability_heuristic(monkeypatch, tmp_path):
    _build(monkeypatch, ["deploy"])
    # a valid skill dir so verify passes
    d = tmp_path / "deploy"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy safely.\n---\n", encoding="utf-8")
    # 4 uses, 100% success -> promotable
    for i in range(4):
        ot.record_outcome(f"s{i}", True, skills=["deploy"])
    a = sg.assess_promotability("deploy", skill_dir=d)
    assert a["promotable"] is True and a["success_rate"] == 1.0 and a["uses"] == 4


def test_assess_blocks_promotion_when_success_hides_tool_thrashing(monkeypatch, tmp_path):
    """A 100%-success skill that only ever succeeds by thrashing must not promote.

    The boolean trajectory cannot see this: every session is a success, so
    success_rate is 1.0 and the skill sails through. The shaped reward can —
    reward = success - 0.5*tool_failure_rate, so a run that succeeded only after
    failing most of its tool calls scores far below a clean one. Gap G10: the
    continuous signal was computed and stored but nothing consumed it, so
    "works, but badly" was indistinguishable from "works".
    """
    _build(monkeypatch, ["thrash"])
    d = tmp_path / "thrash"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: thrash\ndescription: Thrashes.\n---\n", encoding="utf-8")
    for i in range(4):
        ot.record_outcome(f"s{i}", True, skills=["thrash"], tool_failure_rate=1.0)

    a = sg.assess_promotability("thrash", skill_dir=d)

    assert a["success_rate"] == 1.0, "boolean signal is blind to this by construction"
    assert a["mean_reward"] == 0.5, "shaped reward sees the thrashing"
    assert a["promotable"] is False


def test_assess_promotes_clean_skill_on_both_signals(monkeypatch, tmp_path):
    """The reward floor must not block a genuinely clean skill."""
    _build(monkeypatch, ["clean"])
    d = tmp_path / "clean"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: clean\ndescription: Clean.\n---\n", encoding="utf-8")
    for i in range(4):
        ot.record_outcome(f"s{i}", True, skills=["clean"], tool_failure_rate=0.0)

    a = sg.assess_promotability("clean", skill_dir=d)

    assert a["mean_reward"] == 1.0 and a["promotable"] is True


def test_assess_flags_refinement_on_low_success(monkeypatch, tmp_path):
    _build(monkeypatch, ["flaky"])
    d = tmp_path / "flaky"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: flaky\ndescription: Flaky thing.\n---\n", encoding="utf-8")
    for i in range(4):
        ot.record_outcome(f"s{i}", i == 0, skills=["flaky"])  # 25% success
    a = sg.assess_promotability("flaky", skill_dir=d)
    assert a["promotable"] is False and a["refinement_needed"] is True


def test_assess_failed_selftest_flags_refinement(monkeypatch, tmp_path):
    _build(monkeypatch, ["broken"])
    d = tmp_path / "broken"; d.mkdir()  # no SKILL.md -> verify fails
    a = sg.assess_promotability("broken", skill_dir=d)
    assert a["verify_ok"] is False and a["refinement_needed"] is True


def test_promote_and_flag(monkeypatch):
    _build(monkeypatch, ["a"])
    assert sg.promote_skill("a") == {"ok": True, "promotion_level": 1}
    assert sg.promote_skill("a")["promotion_level"] == 2
    assert sg.flag_refinement_needed("a", "needs work") is True
    assert sg.get_node("a")["refinement_flagged"] is True
    assert sg.promote_skill("ghost")["ok"] is False


# ── _activate_draft: same-name replacement (no name-2 twin) ─────────────────

def _seed_active(home, category, name, body):
    d = home / "skills" / category / name if category else home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8")
    return d


def _seed_draft(home, name, body):
    d = home / "skills" / ".drafts" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8")
    return d


def test_activate_draft_replaces_same_name_skill_in_place():
    from janus_constants import get_janus_home
    from agent.skill_utils import iter_skill_index_files, parse_frontmatter
    home = get_janus_home()
    active = _seed_active(home, "devops", "deploy", "OLD")
    draft = _seed_draft(home, "deploy", "NEW")

    moved = sg._activate_draft(draft, "deploy", {}, {"deploy"})

    assert moved == "deploy"                      # no -2 suffix
    # exactly one ACTIVE SKILL.md carries the name, at the original categorized path
    hits = [md for md in iter_skill_index_files(home / "skills", "SKILL.md")
            if (parse_frontmatter(md.read_text(encoding="utf-8"))[0].get("name")
                or md.parent.name) == "deploy"]
    assert hits == [active / "SKILL.md"]
    assert "NEW" in (active / "SKILL.md").read_text(encoding="utf-8")
    # the original was archived, never deleted
    archived = list((home / "skills" / ".archive").glob("deploy-*/SKILL.md"))
    assert len(archived) == 1
    assert "OLD" in archived[0].read_text(encoding="utf-8")
    assert not draft.exists()                     # draft left quarantine


def test_activate_draft_still_suffixes_genuine_directory_collision():
    from janus_constants import get_janus_home
    home = get_janus_home()
    # A DIFFERENT skill occupies the flat 'tool' directory (frontmatter name
    # differs) — activation must not clobber it; the -2 suffix is correct here.
    other = home / "skills" / "tool"
    other.mkdir(parents=True, exist_ok=True)
    (other / "SKILL.md").write_text(
        "---\nname: something-else\ndescription: d\n---\n\nKEEP", encoding="utf-8")
    draft = _seed_draft(home, "tool", "NEW")

    moved = sg._activate_draft(draft, "tool", {}, set())

    assert moved == "tool-2"
    assert "KEEP" in (other / "SKILL.md").read_text(encoding="utf-8")
    assert "NEW" in (home / "skills" / "tool-2" / "SKILL.md").read_text(encoding="utf-8")
