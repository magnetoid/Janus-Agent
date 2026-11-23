"""``janus --resume <value>`` resolves the session before launching anything.

An unknown value (a typo, a session from another profile, a deleted session)
used to open the classic REPL or the TUI anyway. Every message then failed
with "Session not found", and the process still exited 0. Resolution now
happens in ``cmd_chat`` before either surface starts, against a real
``SessionDB`` here, so the exact-ID and title lookups are exercised for real.
"""

from argparse import Namespace
import sqlite3
import sys
import types

import pytest

KNOWN_ID = "20260101_120000_abc123"
KNOWN_TITLE = "refactor the parser"


def _args(surface, **overrides):
    base = {
        "continue_last": None,
        "model": None,
        "provider": None,
        "resume": None,
        "toolsets": None,
        "query": None,
        "tui_dev": False,
        "cli": surface == "cli",
        "tui": surface == "tui",
    }
    base.update(overrides)
    return Namespace(**base)


@pytest.fixture
def session_db_path(tmp_path, monkeypatch):
    """A real state.db holding one titled session; SessionDB() opens it."""
    import janus_state

    db_path = tmp_path / "state.db"
    db = janus_state.SessionDB(db_path=db_path)
    try:
        db.create_session(KNOWN_ID, source="cli")
        db.set_session_title(KNOWN_ID, KNOWN_TITLE)
    finally:
        db.close()
    monkeypatch.setattr(janus_state, "DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def launches(monkeypatch):
    """Stub both chat surfaces and record which one started, with what id."""
    import janus_cli.main as main_mod

    started = []

    def fake_launch_tui(resume_session_id=None, **_kwargs):
        started.append(("tui", resume_session_id))
        raise SystemExit(0)  # the real launcher never returns to cmd_chat

    def fake_cli_main(**kwargs):
        started.append(("cli", kwargs.get("resume")))

    fake_cli = types.ModuleType("cli")
    fake_cli.main = fake_cli_main

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch_tui)
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    # Startup side work that has nothing to do with resume resolution.
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: False)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(
        "janus_cli.first_run.maybe_offer_first_run", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "janus_cli.learning_onboarding.offer", lambda *_a, **_k: None
    )
    return started


def _run_cmd_chat(args):
    import janus_cli.main as main_mod

    try:
        main_mod.cmd_chat(args)
    except SystemExit as exc:
        return exc.code
    return None


@pytest.mark.parametrize("surface", ["cli", "tui"])
def test_unknown_resume_value_exits_before_starting_any_surface(
    surface, session_db_path, launches, capsys
):
    code = _run_cmd_chat(_args(surface, resume="nonexist"))

    captured = capsys.readouterr()
    assert code not in (None, 0)
    assert launches == []
    assert "nonexist" in captured.err
    assert "janus sessions list" in captured.err


@pytest.mark.parametrize("surface", ["cli", "tui"])
@pytest.mark.parametrize("value", [KNOWN_ID, KNOWN_TITLE])
def test_known_resume_id_or_title_still_resumes(
    surface, value, session_db_path, launches, capsys
):
    code = _run_cmd_chat(_args(surface, resume=value))

    assert code in (None, 0)
    assert launches == [(surface, KNOWN_ID)]
    assert "No session found" not in capsys.readouterr().err


def test_unreadable_session_store_is_not_reported_as_unknown_session(
    monkeypatch, launches, capsys
):
    """If state.db can't be opened we can't say the session doesn't exist.

    Keep the value and let the surface report the store problem, rather than
    exit with a misleading "No session found".
    """
    import janus_state

    def broken_session_db(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(janus_state, "SessionDB", broken_session_db)

    code = _run_cmd_chat(_args("cli", resume=KNOWN_ID))

    assert code in (None, 0)
    assert launches == [("cli", KNOWN_ID)]
    assert "No session found" not in capsys.readouterr().err
