"""`janus config get` prints one value and masks secrets unless --raw."""

from __future__ import annotations

from argparse import Namespace

import pytest
import yaml

from janus_cli import config as cfg_mod

_KEY = "sk-or-v1-abcdefghijklmnop12345678"
_PREFIXED = "sk-proj-abcdefghijklmnop1234567890"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    cfg_mod._LOAD_CONFIG_CACHE.clear()
    cfg_mod._RAW_CONFIG_CACHE.clear()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "anthropic/claude-sonnet-4", "api_key": _KEY},
        "providers": {"mine": {"base_url": "https://llm.example.com/v1", "api_key": _KEY,
                               "notes": f"rotated {_PREFIXED}"}},
        "agent": {"max_tokens": 4096},
        "fallback_providers": [{"provider": "openrouter", "api_key": _KEY}],
    }), encoding="utf-8")
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={_KEY}\nJANUS_LOG_LEVEL=debug\n", encoding="utf-8")
    return tmp_path


def _get(capsys, key, raw=False):
    with pytest.raises(SystemExit) as exc:
        cfg_mod.config_command(Namespace(config_command="get", key=key, raw=raw))
    return exc.value.code, capsys.readouterr().out


def test_plain_value_is_printed_as_is(home, capsys):
    code, out = _get(capsys, "model.default")
    assert code == 0 and out.strip() == "anthropic/claude-sonnet-4"


def test_numbers_under_token_like_names_are_not_masked(home, capsys):
    code, out = _get(capsys, "agent.max_tokens")
    assert code == 0 and out.strip() == "4096"


def test_secret_value_is_masked(home, capsys):
    code, out = _get(capsys, "model.api_key")
    assert code == 0 and _KEY not in out and "..." in out


def test_raw_prints_the_secret(home, capsys):
    code, out = _get(capsys, "model.api_key", raw=True)
    assert code == 0 and out.strip() == _KEY


def test_section_masks_every_secret_leaf_and_keeps_the_rest(home, capsys):
    code, out = _get(capsys, "providers.mine")
    shown = yaml.safe_load(out)
    assert code == 0
    assert shown["base_url"] == "https://llm.example.com/v1"
    assert _KEY not in out and _PREFIXED not in out
    assert shown["notes"].startswith("rotated ")


def test_env_key_is_read_from_dotenv_and_masked(home, capsys):
    code, out = _get(capsys, "OPENROUTER_API_KEY")
    assert code == 0 and _KEY not in out and "..." in out
    code, out = _get(capsys, "JANUS_LOG_LEVEL")
    assert code == 0 and out.strip() == "debug"


def test_missing_key_exits_non_zero(home, capsys):
    code, out = _get(capsys, "model.nope")
    assert code == 1 and "not set" in out


def test_numeric_parts_index_lists(home, capsys):
    code, out = _get(capsys, "fallback_providers.0.provider")
    assert code == 0 and out.strip() == "openrouter"
    code, out = _get(capsys, "fallback_providers.0.api_key")
    assert code == 0 and _KEY not in out
    code, out = _get(capsys, "fallback_providers.5.provider")
    assert code == 1
