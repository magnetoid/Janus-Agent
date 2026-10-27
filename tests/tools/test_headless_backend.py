"""Sandboxing for UNATTENDED runs (terminal.headless_backend).

Invariants: the resolver only overrides when configured AND possible; with the
per-task override unset, the terminal backend read is exactly the old
``TERMINAL_ENV`` behaviour; interactive entrypoints never consult the resolver;
and the key is declared identically in both config loaders (3-loader parity).
"""
from __future__ import annotations

import contextvars
import os
import re
from argparse import Namespace
from pathlib import Path

import pytest

from gateway import session_context as sc
from tools.environments import docker as dk
from tools.environments import headless as hl

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_docker_cache():
    dk._reset_docker_usable_cache()
    yield
    dk._reset_docker_usable_cache()


# ── resolver ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cfg", [
    {"terminal": {"headless_backend": "inherit"}},
    {"terminal": {"headless_backend": ""}},
    {"terminal": {"headless_backend": "none"}},
])
def test_inherit_never_overrides(cfg, monkeypatch):
    monkeypatch.setattr(dk, "docker_usable", lambda *a, **k: True)
    assert hl.resolve_headless_backend(cfg) is None


def test_auto_without_docker_is_no_override(monkeypatch):
    monkeypatch.setattr(dk, "docker_usable", lambda *a, **k: False)
    assert hl.resolve_headless_backend({}) is None
    assert hl.resolve_headless_backend({"terminal": {"headless_backend": "auto"}}) is None


def test_auto_with_docker_picks_docker(monkeypatch):
    monkeypatch.setattr(dk, "docker_usable", lambda *a, **k: True)
    assert hl.resolve_headless_backend({}) == "docker"


def test_explicit_backend_passes_through_and_unknown_is_ignored(monkeypatch):
    monkeypatch.setattr(dk, "docker_usable", lambda *a, **k: False)
    assert hl.resolve_headless_backend({"terminal": {"headless_backend": "SSH"}}) == "ssh"
    assert hl.resolve_headless_backend({"terminal": {"headless_backend": "local"}}) == "local"
    assert hl.resolve_headless_backend({"terminal": {"headless_backend": "bogus"}}) is None


def test_default_is_auto_and_garbage_config_is_safe():
    assert hl.headless_backend_setting(None) == "auto"
    assert hl.headless_backend_setting({"terminal": "not-a-dict"}) == "auto"
    assert hl.headless_backend_setting({"terminal": {"headless_backend": None}}) == "auto"


# ── docker_usable is cached and never raises ─────────────────────────────


def test_docker_usable_caches_and_fails_closed(monkeypatch):
    calls = {"n": 0}

    def _find():
        calls["n"] += 1
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(dk, "find_docker", _find)
    assert dk.docker_usable() is False
    assert dk.docker_usable() is False
    assert calls["n"] == 1  # cached after the first probe


# ── terminal_tool reads: contextvar first, then TERMINAL_ENV, else local ──


def test_resolve_terminal_env_is_old_behaviour_when_override_unset(monkeypatch):
    import tools.terminal_tool as tt

    def _in_fresh_context(fn):
        return contextvars.copy_context().run(fn)

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    assert _in_fresh_context(tt._resolve_terminal_env) == "local"
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    assert _in_fresh_context(tt._resolve_terminal_env) == os.getenv("TERMINAL_ENV")


def test_resolve_terminal_env_prefers_per_task_override(monkeypatch):
    import tools.terminal_tool as tt
    monkeypatch.setenv("TERMINAL_ENV", "local")

    def _with_override():
        sc._VAR_MAP["JANUS_TERMINAL_BACKEND"].set("docker")
        first = tt._resolve_terminal_env()
        sc._VAR_MAP["JANUS_TERMINAL_BACKEND"].set("")   # cleared → fall back
        second = tt._resolve_terminal_env()
        return first, second

    assert contextvars.copy_context().run(_with_override) == ("docker", "local")


def test_terminal_backend_var_is_registered_for_session_env():
    assert "JANUS_TERMINAL_BACKEND" in sc._VAR_MAP


# ── CLI entrypoints: only cron run/tick and TTY-less oneshot ─────────────


def _spy(monkeypatch, returns="docker"):
    seen = {"n": 0}

    def _resolve(cfg):
        seen["n"] += 1
        return returns

    monkeypatch.setattr(hl, "resolve_headless_backend", _resolve)
    monkeypatch.setattr("janus_cli.config.load_config", lambda: {})
    return seen


def test_interactive_chat_never_consults_resolver(monkeypatch):
    from janus_cli import main as m
    seen = _spy(monkeypatch)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    m._apply_headless_terminal_backend(Namespace(command="chat", oneshot=None))
    m._apply_headless_terminal_backend(Namespace(command=None, oneshot=None))
    assert seen["n"] == 0 and "TERMINAL_ENV" not in os.environ


def test_cron_run_applies_resolved_backend(monkeypatch):
    from janus_cli import main as m
    seen = _spy(monkeypatch, returns="docker")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    m._apply_headless_terminal_backend(Namespace(command="cron", cron_command="run", oneshot=None))
    assert seen["n"] == 1 and os.environ.get("TERMINAL_ENV") == "docker"


def test_cron_run_leaves_env_alone_when_nothing_resolved(monkeypatch):
    from janus_cli import main as m
    _spy(monkeypatch, returns=None)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    m._apply_headless_terminal_backend(Namespace(command="cron", cron_command="tick", oneshot=None))
    assert "TERMINAL_ENV" not in os.environ


def test_oneshot_only_when_no_tty(monkeypatch):
    import sys
    from janus_cli import main as m
    seen = _spy(monkeypatch)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    m._apply_headless_terminal_backend(Namespace(command=None, oneshot="hi"))
    assert seen["n"] == 0
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    m._apply_headless_terminal_backend(Namespace(command=None, oneshot="hi"))
    assert seen["n"] == 1


# ── 3-loader parity ──────────────────────────────────────────────────────


def test_headless_backend_declared_identically_in_both_loaders():
    from janus_cli.config import DEFAULT_CONFIG
    default_value = DEFAULT_CONFIG["terminal"]["headless_backend"]
    cli_src = (_REPO / "cli.py").read_text(encoding="utf-8")
    m = re.search(r'"headless_backend":\s*"([^"]+)"', cli_src)
    assert m, "load_cli_config defaults must declare terminal.headless_backend"
    assert m.group(1) == default_value
