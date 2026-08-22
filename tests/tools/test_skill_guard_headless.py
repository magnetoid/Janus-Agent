"""Agent-created-skill scanning defaults ON in headless contexts (gap G5 / R2).

An agent-created skill is arbitrary Python that later cron / kanban sessions
load and auto-run on the host. The scan was off by default on the rationale
that "the agent can already run code via terminal() anyway" — true with a human
present, but in a headless context terminal()/execute_code are themselves
BLOCKED by the approval gate, so an unscanned agent skill is the one unguarded
code path there. The guard therefore defaults ON when no human is present
(cron / headless), while an explicit config value still wins in both directions.
"""
from __future__ import annotations

import tools.skill_manager_tool as smt


def _clear_ctx(monkeypatch):
    for var in ("JANUS_INTERACTIVE", "JANUS_CRON_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(smt, "_is_gateway_context", lambda: False)


def test_interactive_session_defaults_guard_off(monkeypatch):
    _clear_ctx(monkeypatch)
    monkeypatch.setenv("JANUS_INTERACTIVE", "1")
    monkeypatch.setattr("janus_cli.config.load_config", lambda: {})
    assert smt._guard_agent_created_enabled() is False


def test_cron_session_defaults_guard_on(monkeypatch):
    _clear_ctx(monkeypatch)
    monkeypatch.setenv("JANUS_CRON_SESSION", "1")
    monkeypatch.setattr("janus_cli.config.load_config", lambda: {})
    assert smt._guard_agent_created_enabled() is True


def test_headless_noninteractive_defaults_guard_on(monkeypatch):
    _clear_ctx(monkeypatch)  # no JANUS_INTERACTIVE, no gateway, no cron
    monkeypatch.setattr("janus_cli.config.load_config", lambda: {})
    assert smt._guard_agent_created_enabled() is True


def test_explicit_config_true_wins_even_interactive(monkeypatch):
    _clear_ctx(monkeypatch)
    monkeypatch.setenv("JANUS_INTERACTIVE", "1")
    monkeypatch.setattr("janus_cli.config.load_config",
                        lambda: {"skills": {"guard_agent_created": True}})
    assert smt._guard_agent_created_enabled() is True


def test_explicit_config_false_wins_even_headless(monkeypatch):
    _clear_ctx(monkeypatch)  # headless context
    monkeypatch.setattr("janus_cli.config.load_config",
                        lambda: {"skills": {"guard_agent_created": False}})
    assert smt._guard_agent_created_enabled() is False


def _write_cfg(tmp_path, body: str):
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
    return tmp_path / "config.yaml"


def test_migration_reclaims_a_materialized_false_default(tmp_path, monkeypatch):
    """Installs predating the "auto" sentinel must not be pinned off forever.

    ``save_config`` writes the FULL merged config back to disk, so every install
    that ever ran it has an explicit ``guard_agent_created: false`` materialized
    from the old default. That literal is indistinguishable from a deliberate
    user choice, so the context default would never apply to an existing user —
    the fix would only help fresh installs. The migration reclaims it.
    """
    cfg = _write_cfg(tmp_path,
                     "_config_version: 28\nskills:\n  guard_agent_created: false\n")
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    from janus_cli.config import migrate_config

    migrate_config(interactive=False, quiet=True)
    assert "guard_agent_created: auto" in cfg.read_text(encoding="utf-8")


def test_migration_preserves_a_deliberate_true(tmp_path, monkeypatch):
    """Only the stale `false` is reclaimed — an explicit opt-in must survive."""
    cfg = _write_cfg(tmp_path,
                     "_config_version: 28\nskills:\n  guard_agent_created: true\n")
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    from janus_cli.config import migrate_config

    migrate_config(interactive=False, quiet=True)
    assert "guard_agent_created: true" in cfg.read_text(encoding="utf-8")


def test_context_default_survives_the_real_merged_config(monkeypatch):
    """The headless default must hold against the REAL config, not just a stub.

    Every other test in this file replaces ``load_config`` with a dict that
    OMITS ``skills.guard_agent_created`` — a shape production never produces,
    because ``load_config`` deep-merges ``DEFAULT_CONFIG`` into every result.
    If the default carries a literal ``False`` there, ``cfg_get(...,
    default=None)`` reads it as an explicit user choice, the "explicit value
    wins" branch swallows it, and every context default above becomes dead
    code. So this test deliberately does NOT patch ``load_config``.
    """
    _clear_ctx(monkeypatch)  # headless: no human present
    assert smt._no_human_present() is True, "precondition: context is headless"
    assert smt._guard_agent_created_enabled() is True
