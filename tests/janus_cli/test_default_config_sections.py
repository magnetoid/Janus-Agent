"""Tool-owned config sections have defaults that change nothing (backlog F5).

``mcp_servers``, ``platform_toolsets``, ``plugins``, ``image_gen`` and
``video_gen`` were read all over the tree but absent from ``DEFAULT_CONFIG``,
so they had no default and no place in generated config. Their defaults must
be the empty value of the type every reader accepts, so a fresh install
behaves exactly as it did when the key was missing.
"""

import pytest

from janus_cli import config as config_mod

_TOOL_OWNED = ("mcp_servers", "platform_toolsets", "plugins", "image_gen", "video_gen")
# Legacy keys still read but migrated away; they must never come back as defaults.
_MIGRATED_AWAY = ("custom_providers", "fallback_model")


def test_known_root_keys_cover_every_default_section():
    assert set(config_mod.DEFAULT_CONFIG) <= config_mod._KNOWN_ROOT_KEYS


@pytest.mark.parametrize("key", _MIGRATED_AWAY)
def test_migrated_legacy_keys_are_known_but_not_defaulted(key):
    assert key in config_mod._KNOWN_ROOT_KEYS
    assert key not in config_mod.DEFAULT_CONFIG


@pytest.mark.parametrize("key", _TOOL_OWNED)
def test_tool_owned_section_defaults_to_an_empty_container(key):
    value = config_mod.DEFAULT_CONFIG[key]
    assert isinstance(value, (dict, list)) and not value


def test_fresh_config_reads_as_nothing_configured():
    from agent.image_gen_registry import get_active_provider
    from janus_cli.plugins import _get_enabled_plugins
    from tools.mcp_tool import _load_mcp_config
    from tools.video_generation_tool import _read_video_gen_section

    cfg = config_mod.load_config()
    for key in _TOOL_OWNED:
        assert cfg[key] == config_mod.DEFAULT_CONFIG[key]

    assert _load_mcp_config() == {}
    assert _get_enabled_plugins() is None          # legacy "not disabled" mode
    assert _read_video_gen_section() == {}
    get_active_provider()                          # must not raise on an empty section


def test_user_values_still_override_the_empty_defaults():
    import yaml

    home = config_mod.get_janus_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "mcp_servers": {"fs": {"command": "npx", "args": ["fs"]}},
        "plugins": {"enabled": ["demo"]},
    }), encoding="utf-8")

    cfg = config_mod.load_config()

    assert cfg["mcp_servers"] == {"fs": {"command": "npx", "args": ["fs"]}}
    assert cfg["plugins"] == {"enabled": ["demo"]}
