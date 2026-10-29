"""`janus memory lint` findings (plan item 3.4)."""
import json
from types import SimpleNamespace

import pytest

from agent import memory_lint, memory_provenance as prov

DAY = 86400.0


def _store(memory=(), user=()):
    return SimpleNamespace(memory_entries=list(memory), user_entries=list(user))


def _kinds(findings):
    return sorted({f["kind"] for f in findings})


def test_offline_lint_finds_duplicates_unknown_origin_untrusted_and_stale(monkeypatch):
    now = 1_800_000_000.0
    records = {
        "Old fact": {"source": "agent", "created_at": now - 400 * DAY, "untrusted": False},
        "Mirror is example.org": {"source": "miner", "created_at": now, "untrusted": True},
        "uses uv": {"source": "agent", "created_at": now, "untrusted": False},
        "Uses UV ": {"source": "agent", "created_at": now, "untrusted": False},
    }
    monkeypatch.setattr(prov, "enabled", lambda: True)
    monkeypatch.setattr(prov, "lookup", lambda target, text: records.get(text))
    store = _store(memory=["Old fact", "Mirror is example.org", "uses uv", "Uses UV ", "No idea where from"])
    findings = memory_lint.lint(store, offline=True, stale_days=180, now=now)
    assert _kinds(findings) == ["duplicate", "stale", "unknown_origin", "untrusted_origin"]
    by_kind = {f["kind"]: f for f in findings}
    assert by_kind["stale"]["entry"] == "Old fact"
    assert by_kind["unknown_origin"]["entry"] == "No idea where from"
    assert by_kind["untrusted_origin"]["entry"] == "Mirror is example.org"


def test_without_provenance_only_content_checks_run(monkeypatch):
    monkeypatch.setattr(prov, "enabled", lambda: False)
    findings = memory_lint.lint(_store(memory=["a", "b"]), offline=True)
    assert findings == []


def test_contradictions_come_from_the_reconcile_check(monkeypatch):
    monkeypatch.setattr(prov, "enabled", lambda: False)

    def llm(**kw):
        payload = [{"drop": 0, "kept": 1, "reason": "moved to staging-2"}]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

    store = _store(memory=["Deploys go to staging-1", "Deploys go to staging-2"])
    findings = memory_lint.lint(store, llm_caller=llm)
    assert [f["kind"] for f in findings] == ["contradiction"]
    assert findings[0]["entry"] == "Deploys go to staging-1"
    assert "staging-2" in findings[0]["detail"]
    assert store.memory_entries == ["Deploys go to staging-1", "Deploys go to staging-2"]  # read-only


def test_cli_prints_findings_and_strict_sets_the_exit_code(monkeypatch, capsys):
    from janus_cli import memory_lint_cli
    monkeypatch.setattr(memory_lint_cli, "_load_store", lambda: _store(memory=["x", "x "]))
    monkeypatch.setattr(prov, "enabled", lambda: False)
    args = SimpleNamespace(target="all", offline=True, stale_days=180, json=False, strict=False)
    assert memory_lint_cli.run(args) == 0
    assert "duplicate" in capsys.readouterr().out
    args.strict = True
    assert memory_lint_cli.run(args) == 1
    capsys.readouterr()
    args.json = True
    memory_lint_cli.run(args)
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "duplicate"
