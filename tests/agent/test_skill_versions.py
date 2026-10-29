"""Invariant tests for agent/skill_versions.py — per-skill history and undo.

The hermetic conftest gives every test its own JANUS_HOME, so the version
store lives under a fresh ``<home>/skills/.versions/``.
"""

from __future__ import annotations

import tarfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest

from agent import skill_versions as sv
from janus_constants import get_janus_home


def _skills() -> Path:
    root = get_janus_home() / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_skill(name: str, body: str = "body", category: str = "") -> Path:
    d = _skills() / category / name if category else _skills() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}\n", encoding="utf-8")
    return d


def _tree(root: Path) -> Dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _store(name: str) -> Path:
    return sv.versions_root() / name


def _live_version_dirs(name: str):
    return sorted(p.name for p in _store(name).iterdir()
                  if p.is_dir() and not p.name.startswith("."))


@pytest.fixture
def keep(monkeypatch):
    def _set(n: int):
        monkeypatch.setattr(sv, "get_keep", lambda: n)
    return _set


@contextmanager
def _skill_manager_on_home():
    """Point skill_manage's module-level SKILLS_DIR at this test's home."""
    root = _skills()
    with patch("tools.skill_manager_tool.SKILLS_DIR", root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[root]):
        yield root


# ---------------------------------------------------------------------------
# Cap + archive-not-delete
# ---------------------------------------------------------------------------

def test_cap_honoured_and_oldest_archived_not_deleted(keep):
    keep(3)
    d = _write_skill("alpha", "v0")
    contents = {}
    for i in range(7):
        vid = sv.record_version("alpha", d, source="skill_manage", reason=f"edit {i}")
        contents[vid] = (d / "SKILL.md").read_bytes()
        (d / "SKILL.md").write_text(f"---\nname: alpha\ndescription: d\n---\n\nv{i + 1}\n",
                                    encoding="utf-8")

    rows = sv.list_versions("alpha")
    assert len(rows) == 7                                   # history never shrinks
    live = [r for r in rows if not r["archived"]]
    archived = [r for r in rows if r["archived"]]
    assert len(live) == 3
    assert set(_live_version_dirs("alpha")) == {r["id"] for r in live}
    # The archived ones are the OLDEST, and every byte is still in the tarball.
    assert {r["id"] for r in archived} == set(list(contents)[:4])
    with tarfile.open(_store("alpha") / ".archive" / "versions.tar") as tf:
        for r in archived:
            member = tf.extractfile(f"{r['id']}/SKILL.md")
            assert member is not None and member.read() == contents[r["id"]]


def test_archived_version_can_still_be_restored(keep):
    keep(1)
    d = _write_skill("beta", "first")
    original = _tree(d)
    first = sv.record_version("beta", d, source="user")
    (d / "SKILL.md").write_text("---\nname: beta\ndescription: d\n---\n\nsecond\n", encoding="utf-8")
    sv.record_version("beta", d, source="user")
    assert sv.resolve_version("beta", first)["archived"] is True

    sv.undo("beta", first)
    assert _tree(d) == original


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def test_undo_restores_byte_exact_and_records_itself():
    d = _write_skill("gamma")
    (d / "SKILL.md").write_bytes(b"---\nname: gamma\ndescription: d\n---\r\n\ttabs  \r\nno-eol")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_bytes(b"print('x')\n")
    (d / "assets").mkdir()
    (d / "assets" / "blob.bin").write_bytes(bytes(range(256)))
    before = _tree(d)

    vid = sv.record_version("gamma", d, source="skill_manage", reason="before edit")
    # Mutate every way: change, add, remove.
    (d / "SKILL.md").write_text("---\nname: gamma\ndescription: d\n---\nchanged\n", encoding="utf-8")
    (d / "references").mkdir()
    (d / "references" / "new.md").write_text("added", encoding="utf-8")
    (d / "assets" / "blob.bin").unlink()
    mutated = _tree(d)

    result = sv.undo("gamma", vid)
    assert _tree(d) == before                               # byte-exact, same file set
    newest = sv.list_versions("gamma")[0]
    assert newest["id"] == result["recorded"]
    assert newest["source"] == "user" and newest["undo_of"] == vid
    # The undone state is kept in history, so undoing again is a redo.
    sv.undo("gamma")
    assert _tree(d) == mutated


def test_undo_of_create_moves_skill_into_history_not_deleted():
    d = _skills() / "delta"
    sv.record_version("delta", d, source="skill_manage", reason="before create")
    _write_skill("delta", "fresh")
    created = _tree(d)

    result = sv.undo("delta")
    assert not d.exists()
    kept = _store("delta") / result["recorded"]
    assert _tree(kept) == created                           # moved, not deleted


def test_failed_restore_puts_current_state_back(monkeypatch):
    d = _write_skill("fragile", "OLD")
    vid = sv.record_version("fragile", d, source="user")
    (d / "SKILL.md").write_text("---\nname: fragile\ndescription: d\n---\n\nCURRENT\n",
                                encoding="utf-8")
    current = _tree(d)
    history = [r["id"] for r in sv.list_versions("fragile")]
    real_copy = sv._copy_tree

    def copy_then_fail(src, dest):
        real_copy(src, dest)          # the destination now exists, partially
        raise OSError("disk full")

    monkeypatch.setattr(sv, "_copy_tree", copy_then_fail)
    with pytest.raises(sv.SkillVersionError, match="put back"):
        sv.undo("fragile", vid)
    assert _tree(d) == current
    assert [r["id"] for r in sv.list_versions("fragile")] == history
    assert set(_live_version_dirs("fragile")) == set(history)   # no orphaned dirs


def test_unrecoverable_restore_keeps_previous_state_in_history(monkeypatch):
    d = _write_skill("stuck", "OLD")
    vid = sv.record_version("stuck", d, source="user")
    (d / "SKILL.md").write_text("---\nname: stuck\ndescription: d\n---\n\nCURRENT\n",
                                encoding="utf-8")
    current = _tree(d)

    def copy_fail(src, dest):
        raise OSError("disk full")

    real_move = sv.shutil.move
    calls = {"n": 0}

    def move_fails_on_recovery(src, dst):
        calls["n"] += 1
        if calls["n"] > 1:            # first move = snapshot aside, second = recovery
            raise OSError("cannot move back")
        return real_move(src, dst)

    monkeypatch.setattr(sv, "_copy_tree", copy_fail)
    monkeypatch.setattr(sv.shutil, "move", move_fails_on_recovery)
    with pytest.raises(sv.SkillVersionError, match="kept in history") as exc:
        sv.undo("stuck", vid)
    kept = sv.list_versions("stuck")[0]
    assert kept["id"] in str(exc.value)
    assert _tree(_store("stuck") / kept["id"]) == current      # still reachable


def test_undo_of_archive_reactivates_lifecycle_state():
    from tools import skill_usage as u
    d = _write_skill("revived")
    data = u.load_usage()
    data["revived"] = u._empty_record()
    data["revived"]["created_by"] = "agent"
    u.save_usage(data)

    sv.archive("revived")
    assert u.get_record("revived")["state"] == u.STATE_ARCHIVED
    sv.undo("revived")
    assert (d / "SKILL.md").exists()
    assert u.get_record("revived")["state"] == u.STATE_ACTIVE


def test_diff_reports_changes_against_current():
    d = _write_skill("eps", "old line")
    sv.record_version("eps", d, source="user")
    (d / "SKILL.md").write_text("---\nname: eps\ndescription: d\n---\n\nnew line\n", encoding="utf-8")
    entry, text = sv.diff("eps")
    assert "-old line" in text and "+new line" in text
    sv.undo("eps", entry["id"])
    assert sv.diff("eps", entry["id"])[1] == ""


def test_version_refs_by_position_and_prefix():
    d = _write_skill("zeta")
    a = sv.record_version("zeta", d, source="user")
    b = sv.record_version("zeta", d, source="curator")
    assert sv.resolve_version("zeta", "1")["id"] == b
    assert sv.resolve_version("zeta", "2")["id"] == a
    assert sv.resolve_version("zeta", a)["id"] == a
    with pytest.raises(sv.SkillVersionError):
        sv.resolve_version("zeta", "9")


def test_record_refuses_path_escaping_names():
    for bad in ("../x", "a/b", ".hidden", ""):
        assert sv.record_version(bad, _skills() / "x", source="user") is None
    root = sv.versions_root()
    assert not root.exists() or list(root.iterdir()) == []


# ---------------------------------------------------------------------------
# Pinned skills
# ---------------------------------------------------------------------------

def test_pinned_skill_is_never_modified_by_undo_or_archive():
    from tools import skill_usage
    d = _write_skill("pinned-one", "keep me")
    sv.record_version("pinned-one", _skills() / "pinned-one", source="user")
    (d / "SKILL.md").write_text("---\nname: pinned-one\ndescription: d\n---\n\ncurrent\n",
                                encoding="utf-8")
    skill_usage.set_pinned("pinned-one", True)
    before = _tree(d)
    history_before = len(sv.list_versions("pinned-one"))

    with pytest.raises(sv.SkillVersionError, match="pinned"):
        sv.undo("pinned-one")
    with pytest.raises(sv.SkillVersionError, match="pinned"):
        sv.archive("pinned-one")

    assert _tree(d) == before
    assert len(sv.list_versions("pinned-one")) == history_before


def test_curator_never_archives_or_versions_a_pinned_skill():
    from agent import curator
    from tools import skill_usage as u
    d = _write_skill("precious")
    old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    data = u.load_usage()
    data["precious"] = u._empty_record()
    data["precious"].update(created_by="agent", last_used_at=old, created_at=old, pinned=True)
    u.save_usage(data)
    before = _tree(d)

    curator.apply_automatic_transitions()
    assert _tree(d) == before
    assert sv.list_versions("precious") == []


# ---------------------------------------------------------------------------
# Every writer path produces a version entry
# ---------------------------------------------------------------------------

_SKILL = "---\nname: wp\ndescription: d\n---\n\nStep 1.\n"


@pytest.mark.parametrize("action, setup, kwargs", [
    ("create", False, {"content": _SKILL}),
    ("edit", True, {"content": _SKILL.replace("Step 1.", "Step 2.")}),
    ("patch", True, {"old_string": "Step 1.", "new_string": "Step 9."}),
    ("write_file", True, {"file_path": "references/a.md", "file_content": "x"}),
    ("remove_file", True, {"file_path": "references/keep.md"}),
    ("delete", True, {"absorbed_into": ""}),
])
def test_every_skill_manage_action_records_a_version(action, setup, kwargs):
    from tools.skill_manager_tool import skill_manage
    with _skill_manager_on_home() as root:
        if setup:
            d = root / "wp"
            d.mkdir()
            (d / "SKILL.md").write_text(_SKILL, encoding="utf-8")
            (d / "references").mkdir()
            (d / "references" / "keep.md").write_text("k", encoding="utf-8")
        before = _tree(root / "wp") if setup else {}
        res = skill_manage(action=action, name="wp", **kwargs)
        assert '"success": true' in res, res

    rows = sv.list_versions("wp")
    assert len(rows) == 1
    assert rows[0]["source"] == "skill_manage" and action in rows[0]["reason"]
    assert rows[0]["absent"] is (not setup)
    if setup:
        assert _tree(_store("wp") / rows[0]["id"]) == before


def test_curator_review_fork_writes_are_attributed_to_curator():
    from tools.skill_manager_tool import skill_manage
    with _skill_manager_on_home() as root:
        d = root / "wp"
        d.mkdir()
        (d / "SKILL.md").write_text(_SKILL, encoding="utf-8")
        with sv.attributed("curator", "curator review pass"):
            skill_manage(action="patch", name="wp", old_string="Step 1.", new_string="Step 2.")
    row = sv.list_versions("wp")[0]
    assert row["source"] == "curator" and "curator review pass" in row["reason"]


def test_curator_auto_archive_records_a_curator_version():
    from agent import curator
    from tools import skill_usage as u
    d = _write_skill("ancient", "old knowledge")
    before = _tree(d)
    old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    data = u.load_usage()
    data["ancient"] = u._empty_record()
    data["ancient"].update(created_by="agent", last_used_at=old, created_at=old)
    u.save_usage(data)

    assert curator.apply_automatic_transitions()["archived"] == 1
    row = sv.list_versions("ancient")[0]
    assert row["source"] == "curator"
    assert _tree(_store("ancient") / row["id"]) == before


def test_skill_miner_draft_write_records_a_version():
    from agent.skill_miner import write_skill_draft
    path = write_skill_draft({"name": "mined", "description": "d", "steps": ["a"], "category": "general",
                              "when_to_use": "w"})
    rows = sv.list_versions(path.name)
    assert len(rows) == 1 and rows[0]["source"] == "skill_miner" and rows[0]["absent"]


def test_draft_activation_over_active_skill_is_undoable():
    from agent.skill_graph import _activate_draft
    live = _write_skill("deploy", "ORIGINAL", category="devops")
    original = _tree(live)
    draft = _skills() / ".drafts" / "deploy"
    draft.mkdir(parents=True)
    (draft / "SKILL.md").write_text("---\nname: deploy\ndescription: d\n---\n\nIMPROVED\n",
                                    encoding="utf-8")

    assert _activate_draft(draft, "deploy", {}, {"deploy"}) == "deploy"
    assert "IMPROVED" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert sv.list_versions("deploy")[0]["source"] == "skill_miner"

    sv.undo("deploy")
    assert _tree(live) == original


def test_self_improve_promotion_records_a_version(monkeypatch):
    from agent import self_improve as si
    monkeypatch.setattr(si, "enabled", lambda config=None: True)
    monkeypatch.setattr(si, "require_human_approval", lambda config=None: True)
    monkeypatch.setattr("agent.evals.load_eval_specs", lambda *a, **k: [object()] * 10)
    monkeypatch.setattr("agent.eval_trend.regression_gate",
                        lambda *a, **k: {"ok": True, "message": "OK"})
    live = _write_skill("flaky", "OLD BODY", category="devops")
    original = _tree(live)
    a = si.propose("skill", "skills/.drafts/flaky/SKILL.md",
                   "---\nname: flaky\ndescription: d\n---\n\nNEW BODY")
    assert a is not None
    si.record_evaluation(a["id"], score_before=0.2, score_after=0.9)
    si.approve(a["id"])
    assert si.promote(a["id"])["promoted"] is True

    row = sv.list_versions("flaky")[0]
    assert row["source"] == "self_improve"
    assert _tree(_store("flaky") / row["id"]) == original


def test_user_archive_and_restore_record_versions():
    from tools import skill_usage as u
    d = _write_skill("shelf")
    data = u.load_usage()
    data["shelf"] = u._empty_record()
    data["shelf"]["created_by"] = "agent"
    u.save_usage(data)

    sv.archive("shelf")
    assert not d.exists()
    sv.restore("shelf")
    assert (d / "SKILL.md").exists()
    rows = sv.list_versions("shelf")
    assert [r["source"] for r in rows] == ["user", "user"]
    assert rows[0]["absent"] is True and rows[1]["absent"] is False


# ---------------------------------------------------------------------------
# Concurrency + isolation from the live skill tree
# ---------------------------------------------------------------------------

def test_index_consistent_under_concurrent_writes(keep):
    keep(4)
    d = _write_skill("busy")
    errors = []

    def worker():
        try:
            for _ in range(5):
                assert sv.record_version("busy", d, source="skill_manage") is not None
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    rows = sv.list_versions("busy")
    ids = [r["id"] for r in rows]
    assert len(ids) == 40 and len(set(ids)) == 40
    live = {r["id"] for r in rows if not r["archived"]}
    assert len(live) == 4 and set(_live_version_dirs("busy")) == live
    with tarfile.open(_store("busy") / ".archive" / "versions.tar") as tf:
        in_tar = {m.name.split("/")[0] for m in tf.getmembers()}
    assert in_tar == {r["id"] for r in rows if r["archived"]}


def test_version_copies_are_never_discovered_as_skills():
    from agent.skill_utils import iter_skill_index_files
    d = _write_skill("solo")
    for _ in range(3):
        sv.record_version("solo", d, source="user")
    found = list(iter_skill_index_files(_skills(), "SKILL.md"))
    assert found == [d / "SKILL.md"]


def test_curator_snapshot_and_rollback_leave_history_alone():
    from agent import curator_backup
    d = _write_skill("hist")
    vid = sv.record_version("hist", d, source="user")
    snap = curator_backup.snapshot_skills(reason="t")
    assert snap is not None
    with tarfile.open(snap / "skills.tar.gz") as tf:
        assert not any(n.startswith(".versions") for n in tf.getnames())
    sv.record_version("hist", d, source="user")
    ok, _msg, _ = curator_backup.rollback(snap.name)
    assert ok
    assert [r["id"] for r in sv.list_versions("hist")][-1] == vid
    assert len(sv.list_versions("hist")) == 2


def test_disabled_versioning_records_nothing(monkeypatch):
    monkeypatch.setattr(sv, "is_enabled", lambda: False)
    d = _write_skill("off")
    assert sv.record_version("off", d, source="user") is None
    assert sv.list_versions("off") == []
