"""`janus gateway obligations` — inspect and replay the delivery ledger."""
import json
from types import SimpleNamespace

import pytest

import janus_cli.gateway as gateway
from gateway.delivery_ledger import DeliveryLedger
from gateway.platforms.base import SendResult
from janus_cli.config import get_janus_home
from janus_state import SessionDB

T0 = 1_800_000_000.0


@pytest.fixture()
def db():
    session_db = SessionDB(db_path=get_janus_home() / "state.db")
    yield session_db
    session_db.close()


def _seed(db):
    ledger = DeliveryLedger(db, clock=lambda: T0)
    owed = ledger.record(kind="reply", platform="telegram", chat_id="42", content="owed reply")
    ledger.settle(owed, SendResult(success=False, error="net down"))
    done = ledger.record(kind="cron", platform="slack", chat_id="C1", content="done", source_ref="job-1")
    ledger.settle(done, SendResult(success=True, message_id="ts"))
    return owed, done


def _args(**kw):
    base = dict(gateway_command="obligations", replay=False, all=False, json=False, limit=50)
    base.update(kw)
    return SimpleNamespace(**base)


def test_json_lists_what_is_owed_and_hides_delivered_unless_all(db, capsys):
    owed, done = _seed(db)
    gateway.gateway_command(_args(json=True))
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [owed]
    assert rows[0]["status"] == "pending" and rows[0]["last_error"] == "net down"

    gateway.gateway_command(_args(json=True, all=True))
    rows = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in rows} == {owed, done}


def test_human_output_names_platform_target_and_attempts(db, capsys):
    _seed(db)
    gateway.gateway_command(_args())
    out = capsys.readouterr().out
    assert "telegram:42" in out
    assert "pending" in out
    assert "net down" in out
    assert "done" not in out                       # delivered rows hidden by default


def test_human_output_when_nothing_is_owed(db, capsys):
    gateway.gateway_command(_args())
    assert "nothing owed" in capsys.readouterr().out.lower()


def test_replay_sends_owed_rows_with_the_standalone_sender(db, capsys, monkeypatch):
    owed, _ = _seed(db)
    sent = []

    async def fake_sender(ob):
        sent.append(ob["id"])
        return SendResult(success=True, message_id="m-replay")

    monkeypatch.setattr(gateway, "_standalone_obligation_sender", lambda: fake_sender)
    # the row's backoff has not elapsed in wall-clock terms; --replay forces it
    gateway.gateway_command(_args(replay=True))
    out = capsys.readouterr().out
    assert sent == [owed]
    assert "1 delivered" in out
    assert db.get_obligation(owed)["status"] == "delivered"
