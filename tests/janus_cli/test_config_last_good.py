"""A broken config.yaml falls back to the last copy that parsed, in every loader.

Each successful parse of the user's config.yaml keeps a verbatim copy beside it
(``config.yaml.last-good``). When a later edit breaks the YAML, the loaders use
that copy instead of dropping every override back to defaults — and they never
rewrite the broken original, which the user still has to fix.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

import cli as cli_mod
from janus_cli import config as cfg_mod

_GOOD = "model:\n  default: last-good/model\n# a comment the copy keeps\nagent:\n  max_turns: 7\n"
_BROKEN = "\tmodel: broken/model\nbroken indent:\n"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    monkeypatch.setattr(cli_mod, "_janus_home", tmp_path)
    cfg_mod._CONFIG_PARSE_WARNED.clear()
    cfg_mod._LOAD_CONFIG_CACHE.clear()
    cfg_mod._RAW_CONFIG_CACHE.clear()
    return tmp_path


def _last_good(home: Path) -> Path:
    return home / "config.yaml.last-good"


def _write(home: Path, text: str) -> None:
    p = home / "config.yaml"
    p.write_text(text, encoding="utf-8")
    # A distinct mtime so the (mtime_ns, size) caches see the edit.
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def _break_after_good_load(home: Path) -> None:
    _write(home, _GOOD)
    cfg_mod.load_config()
    _write(home, _BROKEN)


def test_successful_load_keeps_a_verbatim_copy(home):
    _write(home, _GOOD)
    cfg_mod.load_config()
    assert _last_good(home).read_text(encoding="utf-8") == _GOOD


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_copy_is_private(home):
    # config.yaml can carry credentials (base_url tokens, ${VAR} literals).
    _write(home, _GOOD)
    cfg_mod.load_config()
    assert stat.S_IMODE(_last_good(home).stat().st_mode) == 0o600


def test_load_config_uses_the_copy_when_config_breaks(home, capsys):
    _break_after_good_load(home)
    cfg = cfg_mod.load_config()
    assert cfg["model"]["default"] == "last-good/model"
    assert cfg["agent"]["max_turns"] == 7
    err = capsys.readouterr().err
    assert str(home / "config.yaml") in err and str(_last_good(home)) in err


def test_read_raw_config_uses_the_copy_when_config_breaks(home):
    _break_after_good_load(home)
    assert cfg_mod.read_raw_config()["model"]["default"] == "last-good/model"


def test_cli_loader_uses_the_copy_when_config_breaks(home):
    _break_after_good_load(home)
    assert cli_mod.load_cli_config()["model"]["default"] == "last-good/model"


def test_broken_original_and_copy_are_left_alone(home):
    _break_after_good_load(home)
    cfg_mod.load_config()
    cfg_mod.read_raw_config()
    assert (home / "config.yaml").read_text(encoding="utf-8") == _BROKEN
    assert _last_good(home).read_text(encoding="utf-8") == _GOOD


def test_without_a_copy_the_defaults_path_is_unchanged(home, capsys):
    _write(home, _BROKEN)
    cfg = cfg_mod.load_config()
    assert cfg["model"] == cfg_mod.load_config()["model"]
    assert cfg["agent"]["max_turns"] == cfg_mod.DEFAULT_CONFIG["agent"]["max_turns"]
    assert "Falling back to default config" in capsys.readouterr().err
    assert not _last_good(home).exists()


def test_unparseable_copy_is_ignored(home):
    _write(home, _BROKEN)
    _last_good(home).write_text(_BROKEN, encoding="utf-8")
    cfg = cfg_mod.load_config()
    assert cfg["agent"]["max_turns"] == cfg_mod.DEFAULT_CONFIG["agent"]["max_turns"]


def test_a_fixed_config_replaces_the_copy(home):
    _break_after_good_load(home)
    cfg_mod.load_config()
    fixed = yaml.safe_dump({"model": {"default": "fixed/model"}})
    _write(home, fixed)
    assert cfg_mod.load_config()["model"]["default"] == "fixed/model"
    assert _last_good(home).read_text(encoding="utf-8") == fixed
