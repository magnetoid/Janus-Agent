"""`janus skills history|diff|undo|archive|restore` and the `/skills` slash form.

Drives the version verbs through their real argparse definitions and the
slash dispatcher against the hermetic JANUS_HOME, asserting behaviour on disk
rather than exact output text. Only the wiring tests go through the full
``janus_cli.main.main`` entry point: it rebuilds the whole CLI parser on every
call, which is too slow to repeat under a loaded CI box.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

from agent import skill_versions as sv
from janus_constants import get_janus_home


def _skill(name: str, body: str) -> Path:
    d = get_janus_home() / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}\n",
                                encoding="utf-8")
    return d


def _edit_with_history(name: str, old: str, new: str) -> Path:
    d = _skill(name, old)
    sv.record_version(name, d, source="skill_manage", reason="before edit")
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\n{new}\n",
                                encoding="utf-8")
    return d


def _janus(monkeypatch, *argv: str) -> int:
    """Full `janus` entry point — used only for the wiring tests."""
    from janus_cli.main import main
    monkeypatch.setattr(sys, "argv", ["janus", *argv])
    try:
        main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def _skills(*argv: str) -> int:
    """`janus skills <verb> ...` through the verbs' own argparse definitions."""
    from janus_cli.skill_versions_cli import register_subparsers, run_from_args
    parser = argparse.ArgumentParser(prog="janus skills")
    register_subparsers(parser.add_subparsers(dest="skills_action"))
    return run_from_args(parser.parse_args(list(argv)))


def _slash(cmd: str) -> str:
    from janus_cli.skills_hub import handle_skills_slash
    buf = io.StringIO()
    handle_skills_slash(cmd, Console(file=buf, width=200, force_terminal=False))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# `janus` entry-point wiring
# ---------------------------------------------------------------------------

def test_janus_skills_history_is_wired_into_main(monkeypatch, capsys):
    _edit_with_history("hist", "v1", "v2")
    assert _janus(monkeypatch, "skills", "history", "hist") == 0
    out = capsys.readouterr().out
    for row in sv.list_versions("hist"):
        assert row["id"] in out


def test_janus_skills_undo_version_arg_does_not_hit_global_version_flag(monkeypatch):
    # The positional must not be stored as args.version: main() treats that
    # as `janus --version` and prints the banner instead of undoing.
    d = _edit_with_history("pos", "FIRST", "SECOND")
    sv.record_version("pos", d, source="skill_manage")
    (d / "SKILL.md").write_text("---\nname: pos\ndescription: d\n---\n\nTHIRD\n", encoding="utf-8")
    assert _janus(monkeypatch, "skills", "undo", "pos", "2") == 0
    assert "FIRST" in (d / "SKILL.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# argparse path
# ---------------------------------------------------------------------------

def test_cli_diff_then_undo_restores_previous_content(capsys):
    d = _edit_with_history("cycle", "ORIGINAL", "AGENT EDIT")
    assert _skills("diff", "cycle") == 0
    out = capsys.readouterr().out
    assert "-ORIGINAL" in out and "+AGENT EDIT" in out

    assert _skills("undo", "cycle") == 0
    assert "ORIGINAL" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert sv.list_versions("cycle")[0]["source"] == "user"


def test_cli_undo_refuses_pinned_skill_and_exits_nonzero():
    from tools import skill_usage
    d = _edit_with_history("guarded", "OLD", "NEW")
    skill_usage.set_pinned("guarded", True)
    assert _skills("undo", "guarded") != 0
    assert "NEW" in (d / "SKILL.md").read_text(encoding="utf-8")


def test_cli_archive_then_restore_round_trips():
    from tools import skill_usage as u
    d = _skill("shelved", "content")
    data = u.load_usage()
    data["shelved"] = u._empty_record()
    data["shelved"]["created_by"] = "agent"
    u.save_usage(data)
    before = (d / "SKILL.md").read_bytes()

    assert _skills("archive", "shelved") == 0
    assert not d.exists()
    assert _skills("restore", "shelved") == 0
    assert (d / "SKILL.md").read_bytes() == before


def test_cli_unknown_skill_history_is_not_an_error(capsys):
    assert _skills("history", "never-existed") == 0
    assert "No recorded versions" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Slash path + registry
# ---------------------------------------------------------------------------

def test_slash_history_and_undo_match_cli_behaviour():
    d = _edit_with_history("chat", "BEFORE", "AFTER")
    vid = sv.list_versions("chat")[0]["id"]
    assert vid in _slash("/skills history chat")
    _slash(f"/skills undo chat {vid}")
    assert "BEFORE" in (d / "SKILL.md").read_text(encoding="utf-8")


def test_slash_diff_output_is_not_mangled_by_rich_markup():
    _skill("markup", "x")
    d = get_janus_home() / "skills" / "markup"
    sv.record_version("markup", d, source="user")
    (d / "SKILL.md").write_text("---\nname: markup\ndescription: d\n---\n\n[bold]x[/bold]\n",
                                encoding="utf-8")
    assert "+[bold]x[/bold]" in _slash("/skills diff markup")


@pytest.mark.parametrize("verb", ["history", "diff", "undo", "archive", "restore"])
def test_every_version_verb_is_registered_and_dispatched(verb):
    from janus_cli.commands import resolve_command
    from janus_cli.skill_versions_cli import VERSION_VERBS
    assert verb in VERSION_VERBS
    cmd = resolve_command("skills")
    assert cmd is not None and verb in cmd.subcommands
    # A bare verb prints its usage rather than falling through to "Unknown action".
    out = _slash(f"/skills {verb}")
    assert "Unknown action" not in out and verb in out
