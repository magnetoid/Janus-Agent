"""tools/evals_sync.py — bundled eval seeding respects user intent."""
from pathlib import Path

import pytest

from tools import evals_sync


@pytest.fixture()
def bundled(tmp_path, monkeypatch):
    b = tmp_path / "bundled-evals"
    b.mkdir()
    (b / "a.yaml").write_text("name: a\nprompt: p\nchecks:\n  - type: contains\n    value: x\n",
                              encoding="utf-8")
    (b / "b.yaml").write_text("name: b\nprompt: p\nchecks:\n  - type: contains\n    value: x\n",
                              encoding="utf-8")
    monkeypatch.setenv("JANUS_BUNDLED_EVALS", str(b))
    return b


def _home_evals():
    from janus_constants import get_janus_home
    return get_janus_home() / "evals"


def test_first_sync_seeds_all_files(bundled):
    out = evals_sync.sync_evals(quiet=True)
    assert sorted(out["copied"]) == ["a.yaml", "b.yaml"]
    assert (_home_evals() / "a.yaml").is_file()
    assert (_home_evals() / ".bundled_manifest").is_file()


def test_user_modified_file_is_never_overwritten(bundled):
    evals_sync.sync_evals(quiet=True)
    mine = _home_evals() / "a.yaml"
    mine.write_text("name: a\nprompt: MY EDIT\nchecks:\n  - type: contains\n    value: x\n",
                    encoding="utf-8")
    (bundled / "a.yaml").write_text("name: a\nprompt: UPSTREAM v2\nchecks:\n"
                                    "  - type: contains\n    value: x\n", encoding="utf-8")
    out = evals_sync.sync_evals(quiet=True)
    assert "a.yaml" in out["skipped_modified"]
    assert "MY EDIT" in mine.read_text(encoding="utf-8")


def test_user_deleted_file_is_never_resurrected(bundled):
    evals_sync.sync_evals(quiet=True)
    (_home_evals() / "b.yaml").unlink()
    out = evals_sync.sync_evals(quiet=True)
    assert "b.yaml" in out["skipped_deleted"]
    assert not (_home_evals() / "b.yaml").exists()


def test_upstream_update_reaches_untouched_copy(bundled):
    evals_sync.sync_evals(quiet=True)
    (bundled / "a.yaml").write_text("name: a\nprompt: UPSTREAM v2\nchecks:\n"
                                    "  - type: contains\n    value: x\n", encoding="utf-8")
    out = evals_sync.sync_evals(quiet=True)
    assert "a.yaml" in out["updated"]
    assert "UPSTREAM v2" in (_home_evals() / "a.yaml").read_text(encoding="utf-8")


def test_stale_manifest_entry_is_pruned_but_user_copy_kept(bundled):
    evals_sync.sync_evals(quiet=True)
    (bundled / "b.yaml").unlink()          # upstream stops shipping it
    out = evals_sync.sync_evals(quiet=True)
    assert "b.yaml" in out["pruned"]
    assert (_home_evals() / "b.yaml").is_file()   # user's copy stays


def test_preexisting_user_file_with_bundled_name_is_left_alone(bundled):
    home = _home_evals()
    home.mkdir(parents=True, exist_ok=True)
    (home / "a.yaml").write_text("name: a\nprompt: USER OWNED\nchecks:\n"
                                 "  - type: contains\n    value: x\n", encoding="utf-8")
    out = evals_sync.sync_evals(quiet=True)
    assert "a.yaml" in out["skipped_modified"]
    assert "USER OWNED" in (home / "a.yaml").read_text(encoding="utf-8")
    # b.yaml (no conflict) still seeds
    assert "b.yaml" in out["copied"]
