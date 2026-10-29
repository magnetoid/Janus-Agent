"""`janus delegation profiles` — list and add named subagent profiles (plan item 3.2)."""
import json
from types import SimpleNamespace

import pytest

import janus_cli.delegation_profiles as dp
from janus_cli.config import load_config, save_config


def _args(sub, **kw):
    base = dict(delegation_command="profiles", profiles_command=sub, json=False, name=None,
                model=None, provider=None, base_url=None, api_mode=None, toolsets=None,
                max_iterations=None, budget_usd=None, role=None, force=False,
                interactive=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _set_profiles(profiles):
    cfg = load_config()
    cfg.setdefault("delegation", {})["profiles"] = profiles
    save_config(cfg)


def test_list_shows_configured_profiles(capsys):
    _set_profiles({"cheap": {"model": "cheap/m", "toolsets": ["web"], "budget_usd": 0.5}})
    assert dp.delegation_command(_args("list", json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows["cheap"]["model"] == "cheap/m"

    assert dp.delegation_command(_args("list")) == 0
    out = capsys.readouterr().out
    assert "cheap" in out and "cheap/m" in out and "web" in out


def test_list_with_no_profiles_says_how_to_add_one(capsys):
    _set_profiles({})
    assert dp.delegation_command(_args("list")) == 0
    assert "janus delegation profiles add" in capsys.readouterr().out


def test_add_writes_a_profile_that_delegate_task_can_read(capsys):
    _set_profiles({})
    rc = dp.delegation_command(_args("add", name="research", model="cheap/m",
                                     provider="openrouter", toolsets="web, file",
                                     max_iterations=12, budget_usd=0.75, role="leaf"))
    assert rc == 0
    prof = load_config()["delegation"]["profiles"]["research"]
    assert prof["model"] == "cheap/m"
    assert prof["provider"] == "openrouter"
    assert prof["toolsets"] == ["web", "file"]
    assert prof["max_iterations"] == 12
    assert prof["budget_usd"] == pytest.approx(0.75)
    assert prof["role"] == "leaf"


def test_add_refuses_to_overwrite_without_force(capsys):
    _set_profiles({"research": {"model": "old/m"}})
    assert dp.delegation_command(_args("add", name="research", model="new/m")) == 1
    assert load_config()["delegation"]["profiles"]["research"]["model"] == "old/m"
    assert dp.delegation_command(_args("add", name="research", model="new/m", force=True)) == 0
    assert load_config()["delegation"]["profiles"]["research"]["model"] == "new/m"


@pytest.mark.parametrize("bad", [dict(role="boss"), dict(budget_usd=-1), dict(max_iterations=0),
                                 dict(name="has space")])
def test_add_rejects_invalid_values_and_writes_nothing(bad, capsys):
    _set_profiles({})
    kw = dict(name="p", model="m")
    kw.update(bad)
    assert dp.delegation_command(_args("add", **kw)) == 1
    assert load_config()["delegation"].get("profiles", {}) == {}


def test_remove_deletes_only_the_named_profile(capsys):
    _set_profiles({"a": {"model": "a/m"}, "b": {"model": "b/m"}})
    assert dp.delegation_command(_args("remove", name="a")) == 0
    assert set(load_config()["delegation"]["profiles"]) == {"b"}
    assert dp.delegation_command(_args("remove", name="zzz")) == 1


def test_guided_add_prompts_for_missing_values(monkeypatch, capsys):
    _set_profiles({})
    answers = iter(["guided", "cheap/m", "", "web", "", "0.3", ""])
    monkeypatch.setattr(dp, "_ask", lambda prompt, default="": next(answers) or default)
    assert dp.delegation_command(_args("add", interactive=True)) == 0
    prof = load_config()["delegation"]["profiles"]["guided"]
    assert prof["model"] == "cheap/m"
    assert prof["toolsets"] == ["web"]
    assert prof["budget_usd"] == pytest.approx(0.3)
    assert "provider" not in prof   # blank answers are left out, not stored empty
