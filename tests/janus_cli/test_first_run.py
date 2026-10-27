"""Proactive first-run onboarding (janus_cli/first_run.py).

Invariants, not snapshots: the wizard is offered iff the install is fresh,
unconfigured, interactive, unmanaged, and human-driven; every single gate
flipped suppresses the prompt entirely; it asks exactly once, ever; declining
or interrupting never runs the wizard; and all state lives under JANUS_HOME.
"""
from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import pytest

from janus_cli import first_run as fr

_HEADLESS = ("JANUS_CRON_SESSION", "JANUS_GATEWAY_SESSION", "_JANUS_GATEWAY", "JANUS_KANBAN_TASK")


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    """A fresh, interactive, unmanaged, unconfigured install; captures wizard calls."""
    for name in _HEADLESS + ("JANUS_SKIP_FIRST_RUN", "JANUS_IGNORE_USER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fr, "_is_tty", lambda: True)
    monkeypatch.setattr(fr, "_provider_configured", lambda: False)
    monkeypatch.setattr(fr, "_stamp_path", lambda: tmp_path / ".first_run_offered")
    monkeypatch.setattr("janus_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("janus_cli.config.get_config_path", lambda: tmp_path / "config.yaml")
    calls: list = []
    monkeypatch.setattr("janus_cli.setup.run_setup_wizard", lambda ns: calls.append(ns))
    return calls


def _args(**kw):
    base = {"query": None, "image": None}
    base.update(kw)
    return Namespace(**base)


def test_offers_and_runs_wizard_on_yes(fresh):
    assert fr.maybe_offer_first_run(_args(), input_fn=lambda _p: "y") is True
    assert len(fresh) == 1
    ns = fresh[0]
    assert ns.non_interactive is False and ns.section is None and ns.reset is False


def test_empty_reply_defaults_to_yes(fresh):
    assert fr.maybe_offer_first_run(_args(), input_fn=lambda _p: "") is True
    assert len(fresh) == 1


def test_decline_runs_nothing(fresh):
    assert fr.maybe_offer_first_run(_args(), input_fn=lambda _p: "n") is False
    assert fresh == []


def test_keyboard_interrupt_is_a_decline_not_a_crash(fresh):
    def _boom(_p):
        raise KeyboardInterrupt
    assert fr.maybe_offer_first_run(_args(), input_fn=_boom) is False
    assert fresh == []


def test_asks_exactly_once_ever(fresh):
    prompts = {"n": 0}

    def _count(_p):
        prompts["n"] += 1
        return "n"

    assert fr.maybe_offer_first_run(_args(), input_fn=_count) is False
    assert fr.maybe_offer_first_run(_args(), input_fn=_count) is None
    assert prompts["n"] == 1


@pytest.mark.parametrize("flip", [
    "config_exists", "provider_configured", "not_tty", "managed",
    "skip_env", "ignore_user_config", "query", "image",
    *_HEADLESS,
])
def test_any_single_gate_suppresses_the_prompt(fresh, monkeypatch, tmp_path, flip):
    args = _args()
    if flip == "config_exists":
        (tmp_path / "config.yaml").write_text("model: x\n", encoding="utf-8")
    elif flip == "provider_configured":
        monkeypatch.setattr(fr, "_provider_configured", lambda: True)
    elif flip == "not_tty":
        monkeypatch.setattr(fr, "_is_tty", lambda: False)
    elif flip == "managed":
        monkeypatch.setattr("janus_cli.config.is_managed", lambda: True)
    elif flip == "skip_env":
        monkeypatch.setenv("JANUS_SKIP_FIRST_RUN", "1")
    elif flip == "ignore_user_config":
        monkeypatch.setenv("JANUS_IGNORE_USER_CONFIG", "1")
    elif flip == "query":
        args = _args(query="hello")
    elif flip == "image":
        args = _args(image="/tmp/x.png")
    else:
        monkeypatch.setenv(flip, "1")

    asked = {"n": 0}

    def _never(_p):
        asked["n"] += 1
        return "y"

    assert fr.maybe_offer_first_run(args, input_fn=_never) is None
    assert asked["n"] == 0
    assert fresh == []


def test_stamp_lives_under_janus_home(monkeypatch, tmp_path):
    monkeypatch.setattr("janus_constants.get_janus_home", lambda: tmp_path)
    monkeypatch.setattr(fr, "_stamp_path", fr._stamp_path)  # unpatched real impl
    p = fr._stamp_path()
    assert p.parent == tmp_path
    assert str(Path.home()) not in str(p) or str(tmp_path).startswith(str(Path.home()))


def test_wizard_failure_degrades_to_hints_not_crash(fresh, monkeypatch):
    def _explode(ns):
        raise RuntimeError("wizard blew up")
    monkeypatch.setattr("janus_cli.setup.run_setup_wizard", _explode)
    assert fr.maybe_offer_first_run(_args(), input_fn=lambda _p: "y") is False
