"""Regression tests for terminal config -> env-var bridging.

``terminal_tool._get_env_config()`` reads ALL terminal settings from
``os.environ`` (``TERMINAL_*``).  ``config.yaml`` values therefore have to be
bridged into env vars by THREE separate code paths:

  1. ``cli.load_cli_config()``           — CLI / TUI startup
  2. the ``gateway.core`` import-time bridge — gateway / messaging platforms
  3. ``janus_cli.config.set_config_value`` — one-shot ``janus config set …``

If any one of these is missing a key, the corresponding ``config.yaml``
setting silently does nothing for that entry-point.  This bug already shipped
for ``docker_run_as_host_user`` (gateway + CLI), ``docker_mount_cwd_to_workspace``
(gateway), ``docker_env``, ``docker_volumes`` and ``docker_forward_env``
(``janus config set``).

These tests exercise the *behavior*: write a real ``config.yaml`` into an
isolated ``JANUS_HOME``, run each bridge for real, and assert the setting
comes back out of ``terminal_tool._get_env_config()``.  They deliberately do
**not** inspect the source of the bridging dicts — the earlier source-literal
version of this file broke the moment the gateway bridge moved from
``gateway/run.py`` to ``gateway/core.py`` even though nothing about the
behavior changed.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from dotenv import dotenv_values

import cli as cli_mod
import tools.terminal_tool as terminal_tool
from janus_cli.config import DEFAULT_CONFIG, set_config_value

_TERMINAL_PREFIX = "TERMINAL_"


# -------------------------------------------------------------------------
# Fixtures / plumbing
# -------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_environ():
    """Snapshot ``os.environ`` around each test.

    The bridges under test mutate ``os.environ`` directly (that is the whole
    point of them), and reloading ``gateway.core`` additionally sets
    ``_JANUS_GATEWAY``.  Restore everything so tests stay independent.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def janus_home(monkeypatch, tmp_path) -> Path:
    """An isolated JANUS_HOME that all three bridges read from."""
    home = tmp_path / "bridge_home"
    home.mkdir()
    monkeypatch.setenv("JANUS_HOME", str(home))
    # cli.py resolves its home once at import time.
    monkeypatch.setattr(cli_mod, "_janus_home", home)
    return home


def _write_terminal_config(home: Path, terminal_cfg: dict[str, Any]) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({"terminal": terminal_cfg}, sort_keys=False),
        encoding="utf-8",
    )


def _clear_terminal_env() -> None:
    for name in [n for n in os.environ if n.startswith(_TERMINAL_PREFIX)]:
        del os.environ[name]


def _terminal_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(_TERMINAL_PREFIX)}


def _bridge_via_cli(home: Path, terminal_cfg: dict[str, Any]) -> dict[str, str]:
    """Run the CLI/TUI startup bridge and return the TERMINAL_* vars it set."""
    _write_terminal_config(home, terminal_cfg)
    _clear_terminal_env()
    # cli.py suppresses its TERMINAL_CWD export when it detects it is running
    # inside a gateway process; make sure a previous gateway probe in this
    # same process doesn't leak that marker in.
    os.environ.pop("_JANUS_GATEWAY", None)
    cli_mod.load_cli_config()
    return _terminal_env()


def _bridge_via_gateway(home: Path, terminal_cfg: dict[str, Any]) -> dict[str, str]:
    """Run the gateway startup bridge and return the TERMINAL_* vars it set.

    The gateway bridges config at module-import time, so re-importing the
    module *is* the public way to exercise it.
    """
    _write_terminal_config(home, terminal_cfg)
    _clear_terminal_env()
    import gateway.core as gateway_core

    importlib.reload(gateway_core)
    return _terminal_env()


def _bridge_via_config_set(home: Path, key: str, value: str) -> dict[str, str]:
    """Run ``janus config set terminal.<key> <value>`` and return the
    ``TERMINAL_*`` vars it persisted to ``.env``."""
    _clear_terminal_env()
    set_config_value(f"terminal.{key}", value)
    env_path = home / ".env"
    assert env_path.exists(), "`janus config set` should have written a .env"
    return {
        k: v
        for k, v in dotenv_values(env_path).items()
        if k.startswith(_TERMINAL_PREFIX) and v is not None
    }


def _terminal_tool_view(terminal_env: dict[str, str]) -> dict[str, Any]:
    """What terminal_tool sees given exactly these TERMINAL_* vars."""
    _clear_terminal_env()
    os.environ.update(terminal_env)
    return terminal_tool._get_env_config()


# -------------------------------------------------------------------------
# CLI vs gateway agreement
# -------------------------------------------------------------------------

def _probe_terminal_keys() -> set[str]:
    """Candidate ``terminal.*`` config keys, derived from production sources.

    Union of the documented config surface (``DEFAULT_CONFIG['terminal']``)
    and everything ``terminal_tool`` resolves, plus the handful of keys that
    are real config but absent from both.  Keys that no bridge handles simply
    produce no env var on either side, so an over-broad probe is harmless —
    but a key wired into one bridge and not the other shows up immediately.
    """
    keys = set(DEFAULT_CONFIG.get("terminal", {}))
    keys |= set(terminal_tool._get_env_config())
    keys |= {
        "sandbox_dir",
        "lifetime_seconds",
        "docker_persist_across_processes",
        "docker_orphan_reaper",
        "persistent_shell",
        "sudo_password",
        "ssh_host",
        "ssh_user",
        "ssh_port",
        "ssh_key",
    }
    # Derived outputs of _get_env_config(), not config keys.
    keys -= {"host_cwd", "ssh_persistent", "local_persistent"}
    return keys


def test_cli_and_gateway_bridge_the_same_terminal_settings(janus_home):
    """The CLI and gateway bridges must turn the same config.yaml into the
    same ``TERMINAL_*`` environment.

    Drift between them means a config.yaml setting that "works in CLI mode but
    not gateway mode" (or vice-versa) — the bug class that shipped twice for
    ``docker_run_as_host_user`` / ``docker_mount_cwd_to_workspace``.

    Every probed key gets a unique sentinel value so the comparison ignores
    anything the environment (or a ``.env``) contributed on its own; only
    values that provably came from *our* config.yaml are compared.
    """
    probe = {key: f"probe-{key}" for key in _probe_terminal_keys()}
    sentinels = set(probe.values())
    # Reverse map for a legible failure message.
    by_sentinel = {v: k for k, v in probe.items()}

    cli_env = {
        k: v for k, v in _bridge_via_cli(janus_home, probe).items() if v in sentinels
    }
    gw_env = {
        k: v for k, v in _bridge_via_gateway(janus_home, probe).items() if v in sentinels
    }

    only_cli = {k: by_sentinel[v] for k, v in cli_env.items() if k not in gw_env}
    only_gw = {k: by_sentinel[v] for k, v in gw_env.items() if k not in cli_env}

    assert not only_cli, (
        f"config.yaml keys bridged by cli.load_cli_config() but NOT by the "
        f"gateway bridge in gateway/core.py: {sorted(only_cli.values())} "
        f"(env vars {sorted(only_cli)}).  Wire them into both."
    )
    assert not only_gw, (
        f"config.yaml keys bridged by gateway/core.py but NOT by "
        f"cli.load_cli_config(): {sorted(only_gw.values())} "
        f"(env vars {sorted(only_gw)}).  Wire them into both."
    )
    assert cli_env == gw_env, (
        "cli and gateway bridge the same keys but disagree on the values "
        f"they write: {sorted(k for k in cli_env if cli_env[k] != gw_env[k])}"
    )


def test_cli_and_gateway_actually_bridge_something(janus_home):
    """Guard the guard: if both bridges silently stopped working, the equality
    test above would pass vacuously."""
    probe = {key: f"probe-{key}" for key in _probe_terminal_keys()}
    sentinels = set(probe.values())

    cli_env = {
        k: v for k, v in _bridge_via_cli(janus_home, probe).items() if v in sentinels
    }
    assert len(cli_env) >= 10, (
        f"expected the CLI bridge to export many TERMINAL_* vars, got {cli_env}"
    )


# -------------------------------------------------------------------------
# Per-key end-to-end pins: config.yaml / `janus config set` -> terminal_tool
# -------------------------------------------------------------------------

# (config key, config.yaml value, `janus config set` argument,
#  _get_env_config() key, expected resolved value)
#
# Every probe value differs from the built-in default, so a bridge that drops
# the key fails instead of accidentally matching.
_LOAD_BEARING_KEYS = [
    ("backend", "docker", "docker", "env_type", "docker"),
    ("docker_image", "probe/img:1", "probe/img:1", "docker_image", "probe/img:1"),
    ("docker_run_as_host_user", True, "true", "docker_run_as_host_user", True),
    (
        "docker_mount_cwd_to_workspace",
        True,
        "true",
        "docker_mount_cwd_to_workspace",
        True,
    ),
    (
        "docker_persist_across_processes",
        False,
        "false",
        "docker_persist_across_processes",
        False,
    ),
    ("docker_orphan_reaper", False, "false", "docker_orphan_reaper", False),
    ("docker_env", {"PROBE": "1"}, '{"PROBE": "1"}', "docker_env", {"PROBE": "1"}),
    (
        "docker_volumes",
        ["/probe:/workspace"],
        '["/probe:/workspace"]',
        "docker_volumes",
        ["/probe:/workspace"],
    ),
    (
        "docker_forward_env",
        ["PROBE_TOKEN"],
        '["PROBE_TOKEN"]',
        "docker_forward_env",
        ["PROBE_TOKEN"],
    ),
    ("container_cpu", 3, "3", "container_cpu", 3.0),
    ("container_memory", 2048, "2048", "container_memory", 2048),
    ("container_disk", 4096, "4096", "container_disk", 4096),
    ("container_persistent", False, "false", "container_persistent", False),
]

_LOAD_BEARING_IDS = [row[0] for row in _LOAD_BEARING_KEYS]


@pytest.mark.parametrize(
    "config_key, yaml_value, _cli_arg, result_key, expected",
    _LOAD_BEARING_KEYS,
    ids=_LOAD_BEARING_IDS,
)
def test_cli_startup_bridges_key_to_terminal_tool(
    janus_home, config_key, yaml_value, _cli_arg, result_key, expected
):
    """``terminal.<key>`` in config.yaml must reach terminal_tool in CLI/TUI mode."""
    env = _bridge_via_cli(janus_home, {config_key: yaml_value})
    resolved = _terminal_tool_view(env)
    assert resolved[result_key] == expected, (
        f"terminal.{config_key}={yaml_value!r} in config.yaml did not reach "
        f"terminal_tool via the CLI bridge (got {resolved[result_key]!r}). "
        f"Add it to cli.load_cli_config()'s terminal env mapping."
    )


@pytest.mark.parametrize(
    "config_key, yaml_value, _cli_arg, result_key, expected",
    _LOAD_BEARING_KEYS,
    ids=_LOAD_BEARING_IDS,
)
def test_gateway_startup_bridges_key_to_terminal_tool(
    janus_home, config_key, yaml_value, _cli_arg, result_key, expected
):
    """``terminal.<key>`` in config.yaml must reach terminal_tool in gateway mode."""
    env = _bridge_via_gateway(janus_home, {config_key: yaml_value})
    resolved = _terminal_tool_view(env)
    assert resolved[result_key] == expected, (
        f"terminal.{config_key}={yaml_value!r} in config.yaml did not reach "
        f"terminal_tool via the gateway bridge (got {resolved[result_key]!r}). "
        f"Add it to the terminal env mapping in gateway/core.py."
    )


@pytest.mark.parametrize(
    "config_key, _yaml_value, cli_arg, result_key, expected",
    _LOAD_BEARING_KEYS,
    ids=_LOAD_BEARING_IDS,
)
def test_config_set_bridges_key_to_terminal_tool(
    janus_home, config_key, _yaml_value, cli_arg, result_key, expected
):
    """``janus config set terminal.<key> <value>`` must sync to .env so the
    running process (and its children) pick the change up without a restart."""
    env = _bridge_via_config_set(janus_home, config_key, cli_arg)
    resolved = _terminal_tool_view(env)
    assert resolved[result_key] == expected, (
        f"`janus config set terminal.{config_key} {cli_arg}` did not reach "
        f"terminal_tool (got {resolved[result_key]!r}). Add it to "
        f"_config_to_env_sync in janus_cli/config.py:set_config_value."
    )
