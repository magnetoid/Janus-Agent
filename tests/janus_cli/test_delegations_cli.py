"""`janus delegations list|attach|stop` — inspect and control background children."""
import json
from types import SimpleNamespace

import pytest

import janus_cli.delegations as delegations
from janus_cli.config import get_janus_home
from janus_state import SessionDB

T0 = 1_800_000_000.0


@pytest.fixture()
def db():
    session_db = SessionDB(db_path=get_janus_home() / "state.db")
    yield session_db
    session_db.close()


def _args(sub, **kw):
    base = dict(delegations_command=sub, json=False, all=False, limit=50,
                delegation_id=None, follow=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _seed(db):
    running = db.create_delegation(goal="scan the repo", child_session_id="c-run",
                                   parent_session_id="p1", pid=1, now=T0)
    done = db.create_delegation(goal="write docs", child_session_id="c-done",
                                parent_session_id="p1", pid=1, now=T0 + 1)
    db.finish_delegation(done, status="completed", summary="docs written", now=T0 + 5)
    return running, done


def test_list_shows_live_rows_and_hides_finished_unless_all(db, capsys, monkeypatch):
    monkeypatch.setattr(delegations, "_pid_alive", lambda pid: True)
    running, done = _seed(db)
    delegations.delegations_command(_args("list", json=True))
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [running]
    assert rows[0]["status"] == "running"

    delegations.delegations_command(_args("list", json=True, all=True))
    rows = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in rows} == {running, done}


def test_list_human_output_names_goal_status_and_child_session(db, capsys, monkeypatch):
    monkeypatch.setattr(delegations, "_pid_alive", lambda pid: True)
    _seed(db)
    delegations.delegations_command(_args("list", all=True))
    out = capsys.readouterr().out
    assert "scan the repo" in out and "running" in out and "c-run" in out
    assert "write docs" in out and "completed" in out


def test_list_marks_rows_of_dead_processes_as_orphaned(db, capsys, monkeypatch):
    monkeypatch.setattr(delegations, "_pid_alive", lambda pid: False)
    running, _ = _seed(db)
    delegations.delegations_command(_args("list", json=True, all=True))
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows[running]["status"] == "orphaned"


def test_list_when_nothing_is_running(db, capsys):
    delegations.delegations_command(_args("list"))
    assert "no background delegations" in capsys.readouterr().out.lower()


def test_attach_prints_the_child_transcript(db, capsys, monkeypatch):
    monkeypatch.setattr(delegations, "_pid_alive", lambda pid: True)
    running, _ = _seed(db)
    db.create_session("c-run", "cli")
    db.append_message("c-run", "user", "scan the repo")
    db.append_message("c-run", "assistant", None,
                      tool_calls=[{"function": {"name": "terminal", "arguments": "{}"}}])
    db.append_message("c-run", "tool", "ok 12 files", tool_name="terminal")
    db.append_message("c-run", "assistant", "Found 12 files.")

    delegations.delegations_command(_args("attach", delegation_id=running))
    out = capsys.readouterr().out
    assert "scan the repo" in out
    assert "terminal" in out
    assert "Found 12 files." in out
    assert "running" in out


def test_attach_accepts_an_id_prefix_and_reports_unknown_ids(db, capsys):
    running, _ = _seed(db)
    db.create_session("c-run", "cli")
    db.append_message("c-run", "assistant", "hello from the child")
    delegations.delegations_command(_args("attach", delegation_id=running[:5]))
    assert "hello from the child" in capsys.readouterr().out

    rc = delegations.delegations_command(_args("attach", delegation_id="nope"))
    assert rc == 1
    assert "no delegation" in capsys.readouterr().out.lower()


def test_stop_flags_a_running_row_and_refuses_finished_ones(db, capsys):
    running, done = _seed(db)
    assert delegations.delegations_command(_args("stop", delegation_id=running)) == 0
    assert db.get_delegation(running)["stop_requested"] is True
    assert "stop requested" in capsys.readouterr().out.lower()

    assert delegations.delegations_command(_args("stop", delegation_id=done)) == 1
    assert "not running" in capsys.readouterr().out.lower()


def test_list_shows_lost_rows_without_consuming_their_announcement(db, capsys, monkeypatch):
    monkeypatch.setattr(delegations, "_pid_alive", lambda pid: False)
    running, _ = _seed(db)
    delegations.delegations_command(_args("list", json=True))
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    # the lost row stays in the default listing until someone has been told
    assert rows[running]["status"] == "orphaned"
    assert db.get_delegation(running)["delivered_at"] is None

    delegations.delegations_command(_args("list"))
    out = capsys.readouterr().out
    assert "orphaned" in out and "scan the repo" in out
