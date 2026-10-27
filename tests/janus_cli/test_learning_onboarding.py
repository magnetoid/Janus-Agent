"""Guided first-run opt-in for the read-only learning loop (Phase 2.4).

The loop ships OFF. Instead of silently flipping defaults on upgrade, Janus offers
the READ-ONLY bundle once, interactively, and remembers the answer. These tests
pin: it asks exactly once, it only ever sets the read-only flags (never the
autonomous ones), and it stays silent on any non-interactive / managed / already-
answered surface.
"""
from __future__ import annotations

import re

import pytest

from janus_cli import learning_onboarding as lo


@pytest.fixture()
def interactive(monkeypatch, tmp_path):
    """Make it look like an interactive, unmanaged CLI and capture set_config_value.

    Patches the tty check directly (rather than swapping sys.stdout, which fights
    pytest's output capture) and pins the once-only stamp to a per-test path — the
    isolate fixture is per-file, so without this the stamp written by one test
    would suppress the prompt in the next.
    """
    monkeypatch.setattr(lo, "_is_interactive", lambda: True)
    monkeypatch.setattr("janus_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("janus_cli.config.load_config", lambda: {})
    monkeypatch.setattr(lo, "_stamp_path", lambda: tmp_path / ".optin_prompted")
    sets: list = []
    monkeypatch.setattr("janus_cli.config.set_config_value",
                        lambda k, v: sets.append((k, v)))
    return sets


def test_yes_enables_only_the_readonly_bundle(interactive):
    result = lo.offer(input_fn=lambda _p: "y")
    assert result is True
    keys = {k for k, _ in interactive}
    assert keys == {"learning.track_outcomes", "evals.trend.enabled",
                    "learning.governor.enabled"}
    # Never touches any autonomous / write-side switch.
    assert not any("auto_promote" in k or "self_improve" in k or "playbook" in k
                   or "dialectic" in k or "twin_review" in k or "proposer" in k
                   for k in keys)
    assert all(v == "true" for _, v in interactive)


def test_decline_sets_nothing(interactive):
    assert lo.offer(input_fn=lambda _p: "") is False   # empty == No
    assert interactive == []


def test_it_asks_exactly_once(interactive):
    prompts = {"n": 0}

    def _once(_p):
        prompts["n"] += 1
        return "n"

    assert lo.offer(input_fn=_once) is False
    # Second call is a no-op: the stamp was written on the first ask.
    assert lo.offer(input_fn=_once) is None
    assert prompts["n"] == 1


def test_silent_when_non_interactive(monkeypatch):
    monkeypatch.setattr(lo, "_is_interactive", lambda: False)
    called = {"n": 0}
    assert lo.offer(input_fn=lambda _p: called.__setitem__("n", 1) or "y") is None
    assert called["n"] == 0     # never prompted, never enabled


def test_silent_when_managed(interactive, monkeypatch):
    monkeypatch.setattr("janus_cli.config.is_managed", lambda: True)
    assert lo.offer(input_fn=lambda _p: "y") is None
    assert interactive == []


def test_silent_when_already_enabled(interactive, monkeypatch):
    monkeypatch.setattr("janus_cli.config.load_config",
                        lambda: {"learning": {"track_outcomes": True}})
    assert lo.offer(input_fn=lambda _p: "y") is None
    assert interactive == []


def test_keyboard_interrupt_is_a_decline_not_a_crash(interactive):
    def _boom(_p):
        raise KeyboardInterrupt
    assert lo.offer(input_fn=_boom) is False
    assert interactive == []


# ── wizard fold: apply the preset to the wizard's in-memory dict ─────────────


def _leaf_paths(d, prefix=""):
    out = set()
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= _leaf_paths(v, path)
        else:
            out.add(path)
    return out


def test_apply_readonly_preset_sets_exactly_the_three_keys():
    cfg: dict = {}
    lo.apply_readonly_preset(cfg)
    assert _leaf_paths(cfg) == {"learning.track_outcomes", "learning.governor.enabled",
                                "evals.trend.enabled"}
    assert all(v is True for v in (cfg["learning"]["track_outcomes"],
                                   cfg["learning"]["governor"]["enabled"],
                                   cfg["evals"]["trend"]["enabled"]))


def test_apply_readonly_preset_is_idempotent_and_preserves_other_keys():
    cfg = {"learning": {"reflexion": True, "governor": {"caution_ratio": 0.6}},
           "model": "x"}
    lo.apply_readonly_preset(cfg)
    once = {k: v for k, v in cfg.items()}
    lo.apply_readonly_preset(cfg)
    assert cfg == once
    assert cfg["learning"]["reflexion"] is True
    assert cfg["learning"]["governor"]["caution_ratio"] == 0.6
    assert cfg["model"] == "x"


def test_offer_into_config_yes_applies_to_dict_not_file(interactive):
    cfg: dict = {}
    assert lo.offer_into_config(cfg, input_fn=lambda _p: "y") is True
    assert _leaf_paths(cfg) == {"learning.track_outcomes", "learning.governor.enabled",
                                "evals.trend.enabled"}
    assert interactive == []  # never wrote through set_config_value
    # Never any write-side switch.
    assert not any(re.search(r"auto_promote|playbook|dialectic|self_improve|session_mining", p)
                   for p in _leaf_paths(cfg))


def test_offer_into_config_decline_leaves_dict_untouched(interactive):
    cfg = {"model": "x"}
    assert lo.offer_into_config(cfg, input_fn=lambda _p: "n") is False
    assert cfg == {"model": "x"}


def test_offer_into_config_shares_the_stamp_with_chat_time_offer(interactive):
    cfg: dict = {}
    assert lo.offer_into_config(cfg, input_fn=lambda _p: "n") is False
    # Having been asked in the wizard, the chat-time prompt must never fire.
    assert lo.offer(input_fn=lambda _p: "y") is None
    assert interactive == []


def test_offer_into_config_silent_when_already_on_in_dict(interactive):
    cfg = {"learning": {"track_outcomes": True}}
    asked = {"n": 0}
    assert lo.offer_into_config(cfg, input_fn=lambda _p: asked.__setitem__("n", 1) or "y") is None
    assert asked["n"] == 0


def test_offer_into_config_silent_when_non_interactive(monkeypatch):
    monkeypatch.setattr(lo, "_is_interactive", lambda: False)
    cfg: dict = {}
    assert lo.offer_into_config(cfg, input_fn=lambda _p: "y") is None
    assert cfg == {}
