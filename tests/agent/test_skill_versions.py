"""Per-skill version history with undo (plan item 2.5)."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent import skill_versions as sv
from janus_constants import get_janus_home


@pytest.fixture()
def skills_root():
    root = get_janus_home() / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(sv, "_settings", lambda: {"enabled": True, "max_versions": 3})
    monkeypatch.setattr(sv, "_is_pinned", lambda name: False)


def _skill(root: Path, body="# v1\n", ref="ref one\n") -> Path:
    d = root / "devops" / "deploy"
    (d / "references").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: deploy\ndescription: d\n---\n{body}")
    (d / "references" / "a.md").write_text(ref)
    return d


def _snapshot(d: Path):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_disabled_history_records_nothing(skills_root, monkeypatch):
    monkeypatch.setattr(sv, "_settings", lambda: {"enabled": False, "max_versions": 3})
    d = _skill(skills_root)
    assert sv.record_version(d, reason="edit", actor="test") is None
    assert not (skills_root / ".versions").exists()


def test_undo_restores_the_skill_byte_for_byte(skills_root, enabled):
    d = _skill(skills_root)
    before = _snapshot(d)
    assert sv.record_version(d, reason="edit", actor="test")
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: d\n---\n# rewritten\n")
    (d / "references" / "a.md").unlink()
    (d / "references" / "new.md").write_text("added later\n")

    ok, _msg = sv.restore_version("deploy")
    assert ok
    assert _snapshot(d) == before


def test_undo_is_itself_undoable(skills_root, enabled):
    d = _skill(skills_root)
    sv.record_version(d, reason="edit", actor="test")
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: d\n---\n# v2\n")
    after_edit = _snapshot(d)
    sv.restore_version("deploy")
    # the state that undo replaced is now the newest version
    latest = sv.list_versions("deploy")[0]
    ok, _ = sv.restore_version("deploy", latest["id"])
    assert ok and _snapshot(d) == after_edit


def test_unchanged_content_is_not_recorded_twice(skills_root, enabled):
    d = _skill(skills_root)
    first = sv.record_version(d, reason="a", actor="t")
    second = sv.record_version(d, reason="b", actor="t")
    assert first == second
    assert len(sv.list_versions("deploy")) == 1


def test_cap_moves_old_versions_to_an_archive_instead_of_deleting(skills_root, enabled):
    d = _skill(skills_root)
    ids = []
    for i in range(5):
        (d / "SKILL.md").write_text(f"---\nname: deploy\ndescription: d\n---\n# v{i}\n")
        ids.append(sv.record_version(d, reason=f"edit {i}", actor="t"))
    active = [v["id"] for v in sv.list_versions("deploy")]
    assert active == list(reversed(ids))[:3]
    archived = [v["id"] for v in sv.list_versions("deploy", include_archived=True)
                if v.get("archived")]
    assert sorted(archived) == sorted(ids[:2])
    for vid in ids:   # every version's files still exist somewhere
        assert sv.version_files("deploy", vid)


def test_diff_shows_what_changed_since_a_version(skills_root, enabled):
    d = _skill(skills_root)
    vid = sv.record_version(d, reason="edit", actor="t")
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: d\n---\n# brand new line\n")
    text = sv.diff_version("deploy", vid)
    assert "-# v1" in text and "+# brand new line" in text


def test_restore_recreates_a_deleted_skill_where_it_was(skills_root, enabled):
    d = _skill(skills_root)
    before = _snapshot(d)
    sv.record_version(d, reason="delete", actor="t")
    import shutil
    shutil.rmtree(d)
    ok, _ = sv.restore_version("deploy")
    assert ok and _snapshot(d) == before


def test_restore_of_a_pinned_skill_needs_force(skills_root, enabled, monkeypatch):
    d = _skill(skills_root)
    sv.record_version(d, reason="edit", actor="t")
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: d\n---\n# v2\n")
    monkeypatch.setattr(sv, "_is_pinned", lambda name: True)
    ok, msg = sv.restore_version("deploy")
    assert not ok and "pinned" in msg
    assert "# v2" in (d / "SKILL.md").read_text()
    ok, _ = sv.restore_version("deploy", force=True)
    assert ok and "# v1" in (d / "SKILL.md").read_text()


def test_version_copies_are_never_discovered_as_live_skills(skills_root, enabled):
    from agent.skill_utils import is_excluded_skill_path
    d = _skill(skills_root)
    sv.record_version(d, reason="edit", actor="t")
    copies = list((skills_root / ".versions").rglob("SKILL.md"))
    assert copies and all(is_excluded_skill_path(p) for p in copies)


def test_skill_manage_records_a_version_before_each_mutation(skills_root, enabled):
    from tools.skill_manager_tool import skill_manage
    content = "---\nname: hooked\ndescription: d\n---\n# one\n"
    with patch("tools.skill_manager_tool.SKILLS_DIR", skills_root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_root]):
        assert json.loads(skill_manage("create", "hooked", content=content))["success"]
        assert sv.list_versions("hooked") == []            # nothing existed before create
        assert json.loads(skill_manage("patch", "hooked", old_string="# one",
                                       new_string="# two"))["success"]
        assert json.loads(skill_manage("write_file", "hooked", file_path="references/x.md",
                                       file_content="x"))["success"]
    versions = sv.list_versions("hooked")
    assert len(versions) == 2
    ok, _ = sv.restore_version("hooked", versions[-1]["id"])
    assert ok
    assert "# one" in (skills_root / "hooked" / "SKILL.md").read_text()
