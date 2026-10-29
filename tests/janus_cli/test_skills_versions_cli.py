"""`janus skills history|diff|undo` (plan item 2.5)."""
import json
from types import SimpleNamespace

import pytest

from agent import skill_versions as sv
from janus_cli import skills_versions_cli as cli
from janus_constants import get_janus_home


@pytest.fixture()
def skill(monkeypatch):
    monkeypatch.setattr(sv, "_settings", lambda: {"enabled": True, "max_versions": 10})
    monkeypatch.setattr(sv, "_is_pinned", lambda name: False)
    d = get_janus_home() / "skills" / "deploy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: d\n---\n# v1\n")
    sv.record_version(d, reason="patch", actor="skill_manage")
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: d\n---\n# v2\n")
    return d


def _args(action, **kw):
    base = dict(skills_action=action, name="deploy", version=None, json=False, force=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_history_lists_versions_with_reason_and_actor(skill, capsys):
    assert cli.run(_args("history", json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["reason"] == "patch" and rows[0]["actor"] == "skill_manage"
    assert cli.run(_args("history")) == 0
    out = capsys.readouterr().out
    assert rows[0]["id"] in out and "patch" in out


def test_diff_and_undo(skill, capsys):
    assert cli.run(_args("diff")) == 0
    assert "+# v2" in capsys.readouterr().out
    assert cli.run(_args("undo")) == 0
    assert "# v1" in (skill / "SKILL.md").read_text()


def test_unknown_skill_or_version_is_an_error(skill, capsys):
    assert cli.run(_args("history", name="nope")) == 1
    assert cli.run(_args("undo", version="20000101T000000000000Z")) == 1


def test_disabled_history_explains_how_to_enable(monkeypatch, capsys):
    monkeypatch.setattr(sv, "_settings", lambda: {"enabled": False, "max_versions": 10})
    assert cli.run(_args("history")) == 1
    assert "skills.versions.enabled" in capsys.readouterr().out
