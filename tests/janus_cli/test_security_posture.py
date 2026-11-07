"""Configuration-posture engine for `janus security audit` (janus_cli/security_posture.py).

Invariants: every seeded misconfiguration yields exactly its finding and a clean
config yields none; --fix repairs only registry keys, is idempotent, and backs
up once; money is never written without --set-budget-caps; the posture path is
fully offline (works with the OSV engine broken).
"""
from __future__ import annotations

import json
import os
import stat
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from janus_cli import security_posture as sp

_BAD = {
    "skills": {"guard_agent_created": False, "inline_shell": True},
    "approvals": {"mode": "off", "cron_mode": "approve", "headless_mode": "approve"},
    "learning": {
        "governor": {"enabled": False, "auto_promote": True},
        "self_improve": {"require_human_approval": False},
    },
    "terminal": {"backend": "local", "headless_backend": "inherit"},
    "budget": {"session_cost_usd": 0, "daily_usd": 0, "monthly_usd": 0},
    "dashboard": {"host": "0.0.0.0"},
    "platforms": {"telegram": {"allow_from": [], "unauthorized_dm_behavior": "ignore"}},
    "command_allowlist": ["sudo rm -rf /tmp/x"],
    "experimental": {"dangerously_allow_everything": True},
}

_EXPECTED_IDS = {
    "skills.guard_agent_created_off", "approvals.mode_off",
    "approvals.cron_mode_approve", "approvals.headless_mode_approve", "env.yolo_mode",
    "learning.auto_promote_without_governor", "learning.self_improve_no_human",
    "skills.inline_shell", "terminal.sandbox_off_unattended", "budget.no_caps_unattended",
    "autonomy.frozen", "network.exposed_bind", "gateway.allowlist_empty",
    "files.permissions", "command_allowlist.broad", "config.dangerously_prefixed",
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text("[]", encoding="utf-8")   # unattended surface
    monkeypatch.setattr("janus_cli.kanban_db.kanban_db_path", lambda board=None: tmp_path / "nope.db", raising=False)
    monkeypatch.setattr("tools.environments.docker.docker_usable", lambda *a, **k: True)
    monkeypatch.setattr("agent.autonomy_guard.frozen", lambda config=None: True)
    monkeypatch.setattr("agent.autonomy_guard.freeze_reason_text", lambda: "test freeze")
    monkeypatch.setenv("JANUS_YOLO_MODE", "1")
    return tmp_path


def _write_cfg(home: Path, data: dict, mode: int = 0o644) -> Path:
    p = home / "config.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    os.chmod(p, mode)
    return p


# ── detection ────────────────────────────────────────────────────────────


def test_every_seeded_misconfig_is_found(home):
    _write_cfg(home, _BAD, mode=0o644)
    ids = {f.check_id for f in sp.collect(_BAD, _BAD, home, os.environ)}
    assert ids == _EXPECTED_IDS


def test_clean_config_has_no_findings(tmp_path, monkeypatch):
    monkeypatch.delenv("JANUS_YOLO_MODE", raising=False)
    monkeypatch.setattr("agent.autonomy_guard.frozen", lambda config=None: False)
    monkeypatch.setattr("janus_cli.kanban_db.kanban_db_path", lambda board=None: tmp_path / "nope.db", raising=False)
    clean = {"approvals": {"mode": "manual"}, "terminal": {"backend": "local"}}
    _write_cfg(tmp_path, clean, mode=0o600)
    assert sp.collect(clean, clean, tmp_path, {}) == []


def test_sandbox_finding_is_only_fixable_when_docker_is_usable(home, monkeypatch):
    def _one(usable):
        monkeypatch.setattr("tools.environments.docker.docker_usable", lambda *a, **k: usable)
        return next(f for f in sp.collect(_BAD, _BAD, home, {}) if f.check_id == "terminal.sandbox_off_unattended")
    assert _one(True).fixable is True
    assert _one(False).fixable is False


def test_collect_never_raises_on_garbage(home):
    garbage = {"skills": "nope", "approvals": 7, "platforms": ["x"], "learning": None}
    sp.collect(garbage, garbage, home, {})  # must not raise


# ── fixing ───────────────────────────────────────────────────────────────


def _fixable_ids(findings):
    return {f.check_id for f in findings if f.fixable}


def test_fix_repairs_only_registry_keys_and_is_idempotent(home, monkeypatch):
    cfg_path = _write_cfg(home, _BAD, mode=0o644)
    before = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    findings = sp.collect(_BAD, _BAD, home, {})
    applied = sp.apply_fixes(findings, config_path=cfg_path, dry_run=False)
    assert applied, "expected fixes to be applied"
    after = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    # Only keys named by fixable findings changed.
    def _leaves(d, prefix=""):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                yield from _leaves(v, p)
            else:
                yield p, v
    changed = {k for k, v in _leaves(after) if dict(_leaves(before)).get(k) != v} | \
              {k for k in dict(_leaves(before)) if k not in dict(_leaves(after))}
    fixed_keys = {f.key for f in applied if f.key and f.check_id != "files.permissions"}
    assert changed <= fixed_keys, f"unexpected changes: {changed - fixed_keys}"
    # Money is never written without the explicit flag.
    assert after["budget"]["daily_usd"] == 0

    # File permission fix applied.
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    # One backup created.
    assert len(list(home.glob("config.yaml.bak.*"))) == 1

    # Re-collect on the repaired config: no fixable config findings remain
    # (budget stays as advisory; sandbox was fixed via headless_backend).
    again = sp.collect(after, after, home, {})
    assert not any(f.fixable and f.check_id != "files.permissions" for f in again)

    # Idempotent: a second apply changes nothing and adds no backup.
    bytes_before = cfg_path.read_bytes()
    assert sp.apply_fixes(again, config_path=cfg_path, dry_run=False) == []
    assert cfg_path.read_bytes() == bytes_before
    assert len(list(home.glob("config.yaml.bak.*"))) == 1


def test_dry_run_reports_without_writing(home):
    cfg_path = _write_cfg(home, _BAD, mode=0o644)
    raw_before = cfg_path.read_bytes()
    findings = sp.collect(_BAD, _BAD, home, {})
    planned = sp.apply_fixes(findings, config_path=cfg_path, dry_run=True)
    assert planned and cfg_path.read_bytes() == raw_before
    assert not list(home.glob("config.yaml.bak.*"))


def test_budget_caps_only_with_explicit_flag(home):
    cfg_path = _write_cfg(home, _BAD, mode=0o600)
    findings = sp.collect(_BAD, _BAD, home, {})
    sp.apply_fixes(findings, config_path=cfg_path, dry_run=False, set_budget_caps=True)
    after = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert after["budget"]["daily_usd"] == 25 and after["budget"]["monthly_usd"] == 200


# ── CLI: offline path is independent of the OSV engine ───────────────────


def test_posture_only_json_works_with_osv_broken(home, monkeypatch, capsys):
    from janus_cli import security_audit as sa
    cfg_path = _write_cfg(home, _BAD, mode=0o644)
    monkeypatch.setattr("janus_cli.config.load_config", lambda: _BAD)
    monkeypatch.setattr("janus_cli.config.read_raw_config", lambda: _BAD)
    monkeypatch.setattr("janus_cli.config.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("janus_cli.config.is_managed", lambda: False)
    monkeypatch.setattr(sa, "get_janus_home", lambda: home, raising=False)
    monkeypatch.setattr("janus_constants.get_janus_home", lambda: home)

    def _boom(*a, **k):
        raise AssertionError("OSV engine must not run with --posture-only")
    monkeypatch.setattr(sa, "run_audit", _boom)
    monkeypatch.setattr(sa, "_count_components", _boom)

    code = sa.cmd_security_audit(Namespace(
        skip_venv=False, skip_plugins=False, skip_mcp=False, json=True, fail_on="critical",
        fix=False, dry_run=False, posture_only=True, no_posture=False, set_budget_caps=False))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "posture" in out and out["findings"] == []
    assert {f["check_id"] for f in out["posture"]} == _EXPECTED_IDS
    assert code == 1   # a CRITICAL posture finding (self_improve_no_human) trips the default gate


def test_no_posture_preserves_legacy_exit_semantics(home, monkeypatch, capsys):
    from janus_cli import security_audit as sa
    monkeypatch.setattr(sa, "get_janus_home", lambda: home, raising=False)
    monkeypatch.setattr("janus_constants.get_janus_home", lambda: home)
    monkeypatch.setattr(sa, "_count_components", lambda **k: 0)
    code = sa.cmd_security_audit(Namespace(
        skip_venv=True, skip_plugins=True, skip_mcp=True, json=True, fail_on="critical",
        fix=False, dry_run=False, posture_only=False, no_posture=True, set_budget_caps=False))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["total_components_scanned"] == 0 and out["posture"] == []
    assert code == 0


def test_gateway_approver_any_is_reported_and_fixable(tmp_path, monkeypatch):
    monkeypatch.delenv("JANUS_YOLO_MODE", raising=False)
    monkeypatch.setattr("agent.autonomy_guard.frozen", lambda config=None: False)
    monkeypatch.setattr("janus_cli.kanban_db.kanban_db_path", lambda board=None: tmp_path / "nope.db", raising=False)
    cfg = {"approvals": {"mode": "smart", "gateway_approver": "any"}, "terminal": {"backend": "local"}}
    _write_cfg(tmp_path, cfg, mode=0o600)
    findings = {f.check_id: f for f in sp.collect(cfg, cfg, tmp_path, {})}
    assert set(findings) == {"approvals.gateway_approver_any"}
    f = findings["approvals.gateway_approver_any"]
    assert f.fixable and f.safe_value == "requester"


# ── secret files beyond .env/config.yaml, bind hosts from the environment ──


@pytest.fixture()
def quiet_home(tmp_path, monkeypatch):
    """A home whose config is clean, so only the check under test can fire."""
    monkeypatch.delenv("JANUS_YOLO_MODE", raising=False)
    monkeypatch.setattr("agent.autonomy_guard.frozen", lambda config=None: False)
    monkeypatch.setattr("janus_cli.kanban_db.kanban_db_path", lambda board=None: tmp_path / "nope.db", raising=False)
    _write_cfg(tmp_path, _CLEAN, mode=0o600)
    return tmp_path


_CLEAN = {"approvals": {"mode": "manual"}, "terminal": {"backend": "local"}}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("rel", ["auth.json", ".anthropic_oauth.json", "auth/google_oauth.json",
                                 "mcp-tokens/github.json"])
def test_credential_store_readable_by_others_is_reported_and_fixable(quiet_home, rel):
    p = quiet_home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    os.chmod(p, 0o644)
    perms = [f for f in sp.collect(_CLEAN, _CLEAN, quiet_home, {}) if f.check_id == "files.permissions"]
    assert [Path(f.current) for f in perms] == [p]
    applied = sp.apply_fixes(perms, config_path=quiet_home / "config.yaml")
    assert applied == perms and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not [f for f in sp.collect(_CLEAN, _CLEAN, quiet_home, {}) if f.check_id == "files.permissions"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_private_credential_store_is_not_reported(quiet_home):
    p = quiet_home / "auth.json"
    p.write_text("{}", encoding="utf-8")
    os.chmod(p, 0o600)
    assert sp.collect(_CLEAN, _CLEAN, quiet_home, {}) == []


def test_exposed_host_in_environment_is_reported_when_config_is_silent(quiet_home):
    findings = sp.collect(_CLEAN, _CLEAN, quiet_home, {"API_SERVER_HOST": "0.0.0.0"})
    exposed = [f for f in findings if f.check_id == "network.exposed_bind"]
    assert len(exposed) == 1
    f = exposed[0]
    assert "API_SERVER_HOST" in f.title
    # .env and the environment are never edited by --fix
    assert f.fixable is False and f.key is None


def test_exposed_host_in_dotenv_is_reported(quiet_home):
    (quiet_home / ".env").write_text("LINE_HOST='::'\nEMAIL_IMAP_HOST=imap.example.com\n", encoding="utf-8")
    os.chmod(quiet_home / ".env", 0o600)
    exposed = [f for f in sp.collect(_CLEAN, _CLEAN, quiet_home, {}) if f.check_id == "network.exposed_bind"]
    assert ["LINE_HOST" in f.title for f in exposed] == [True]


def test_host_in_both_environment_and_dotenv_is_reported_once(quiet_home):
    (quiet_home / ".env").write_text("API_SERVER_HOST=0.0.0.0\n", encoding="utf-8")
    os.chmod(quiet_home / ".env", 0o600)
    exposed = [f for f in sp.collect(_CLEAN, _CLEAN, quiet_home, {"API_SERVER_HOST": "0.0.0.0"})
               if f.check_id == "network.exposed_bind"]
    assert len(exposed) == 1


def test_localhost_and_remote_hosts_in_environment_are_fine(quiet_home):
    env = {"API_SERVER_HOST": "127.0.0.1", "EMAIL_SMTP_HOST": "smtp.example.com", "HOSTNAME": "0.0.0.0"}
    assert sp.collect(_CLEAN, _CLEAN, quiet_home, env) == []


@pytest.mark.parametrize("name,platform,key", [
    ("FEISHU_WEBHOOK_HOST", "feishu", "webhook_host"),
    ("BLUEBUBBLES_WEBHOOK_HOST", "bluebubbles", "webhook_host"),
    ("API_SERVER_HOST", "api_server", "host"),
])
def test_config_host_that_overrides_the_env_var_suppresses_the_finding(quiet_home, name, platform, key):
    cfg = {**_CLEAN, "platforms": {platform: {"extra": {key: "127.0.0.1"}}}}
    exposed = [f for f in sp.collect(cfg, cfg, quiet_home, {name: "0.0.0.0"})
               if f.check_id == "network.exposed_bind"]
    assert exposed == []


@pytest.mark.parametrize("name,platform,key,seed", [
    ("API_SERVER_HOST", "api_server", "host", {"API_SERVER_ENABLED": "true"}),
    ("API_SERVER_HOST", "api_server", "host", {"API_SERVER_KEY": "k"}),
    ("BLUEBUBBLES_WEBHOOK_HOST", "bluebubbles", "webhook_host",
     {"BLUEBUBBLES_SERVER_URL": "http://bb", "BLUEBUBBLES_PASSWORD": "pw"}),
])
def test_env_seeded_platform_keeps_the_env_host_finding(quiet_home, name, platform, key, seed):
    # gateway/config.py copies the env host over config extra in these cases.
    cfg = {**_CLEAN, "platforms": {platform: {"extra": {key: "127.0.0.1"}}}}
    exposed = [f for f in sp.collect(cfg, cfg, quiet_home, {name: "0.0.0.0", **seed})
               if f.check_id == "network.exposed_bind"]
    assert len(exposed) == 1


def test_env_wins_hosts_are_reported_despite_config(quiet_home):
    # LINE reads LINE_HOST before its config extra.
    cfg = {**_CLEAN, "platforms": {"line": {"extra": {"host": "127.0.0.1"}}}}
    exposed = [f for f in sp.collect(cfg, cfg, quiet_home, {"LINE_HOST": "0.0.0.0"})
               if f.check_id == "network.exposed_bind"]
    assert len(exposed) == 1
