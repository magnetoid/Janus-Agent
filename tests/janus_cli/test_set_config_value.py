"""Tests for set_config_value — verifying secrets route to .env and config to config.yaml."""

import argparse
import os
from unittest.mock import patch

import pytest

from janus_cli.config import set_config_value, config_command


@pytest.fixture(autouse=True)
def _isolated_janus_home(tmp_path):
    """Point JANUS_HOME at a temp dir so tests never touch real config."""
    env_file = tmp_path / ".env"
    env_file.touch()
    with patch.dict(os.environ, {"JANUS_HOME": str(tmp_path)}):
        yield tmp_path


def _read_env(tmp_path):
    return (tmp_path / ".env").read_text()


def _read_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    return config_path.read_text() if config_path.exists() else ""


# ---------------------------------------------------------------------------
# Explicit allowlist keys → .env
# ---------------------------------------------------------------------------

class TestExplicitAllowlist:
    """Keys in the hardcoded allowlist should always go to .env."""

    @pytest.mark.parametrize("key", [
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HONCHO_API_KEY",
        "FIRECRAWL_API_KEY",
        "BROWSERBASE_API_KEY",
        "FAL_KEY",
        "SUDO_PASSWORD",
        "GITHUB_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
    ])
    def test_explicit_key_routes_to_env(self, key, _isolated_janus_home):
        set_config_value(key, "test-value-123")
        env_content = _read_env(_isolated_janus_home)
        assert f"{key}=test-value-123" in env_content
        # Must NOT appear in config.yaml
        assert key not in _read_config(_isolated_janus_home)


# ---------------------------------------------------------------------------
# Catch-all patterns → .env
# ---------------------------------------------------------------------------

class TestCatchAllPatterns:
    """Any key ending in _API_KEY or _TOKEN should route to .env."""

    @pytest.mark.parametrize("key", [
        "DAYTONA_API_KEY",
        "ELEVENLABS_API_KEY",
        "SOME_FUTURE_SERVICE_API_KEY",
        "MY_CUSTOM_TOKEN",
        "WHATSAPP_BOT_TOKEN",
    ])
    def test_api_key_suffix_routes_to_env(self, key, _isolated_janus_home):
        set_config_value(key, "secret-456")
        env_content = _read_env(_isolated_janus_home)
        assert f"{key}=secret-456" in env_content
        assert key not in _read_config(_isolated_janus_home)

    def test_case_insensitive(self, _isolated_janus_home):
        """Keys should be uppercased regardless of input casing."""
        set_config_value("openai_api_key", "sk-test")
        env_content = _read_env(_isolated_janus_home)
        assert "OPENAI_API_KEY=sk-test" in env_content

    def test_terminal_ssh_prefix_routes_to_env(self, _isolated_janus_home):
        set_config_value("TERMINAL_SSH_PORT", "2222")
        env_content = _read_env(_isolated_janus_home)
        assert "TERMINAL_SSH_PORT=2222" in env_content


# ---------------------------------------------------------------------------
# Non-secret keys → config.yaml
# ---------------------------------------------------------------------------

class TestConfigYamlRouting:
    """Regular config keys should go to config.yaml, NOT .env."""

    def test_simple_key(self, _isolated_janus_home):
        set_config_value("model", "gpt-4o")
        config = _read_config(_isolated_janus_home)
        assert "gpt-4o" in config
        assert "model" not in _read_env(_isolated_janus_home)

    def test_nested_key(self, _isolated_janus_home):
        set_config_value("terminal.backend", "docker")
        config = _read_config(_isolated_janus_home)
        assert "docker" in config
        assert "terminal" not in _read_env(_isolated_janus_home)

    def test_terminal_image_goes_to_config(self, _isolated_janus_home):
        """TERMINAL_DOCKER_IMAGE doesn't match _API_KEY or _TOKEN, so config.yaml."""
        set_config_value("terminal.docker_image", "python:3.12")
        config = _read_config(_isolated_janus_home)
        assert "python:3.12" in config

    def test_terminal_docker_cwd_mount_flag_goes_to_config_and_env(self, _isolated_janus_home):
        set_config_value("terminal.docker_mount_cwd_to_workspace", "true")
        config = _read_config(_isolated_janus_home)
        env_content = _read_env(_isolated_janus_home)
        assert "docker_mount_cwd_to_workspace: 'true'" in config or "docker_mount_cwd_to_workspace: true" in config
        assert (
            "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE=true" in env_content
            or "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE=True" in env_content
        )


# ---------------------------------------------------------------------------
# Empty / falsy values — regression tests for #4277
# ---------------------------------------------------------------------------

class TestFalsyValues:
    """config set should accept empty strings and falsy values like '0'."""

    def test_empty_string_routes_to_env(self, _isolated_janus_home):
        """Blanking an API key should write an empty value to .env."""
        set_config_value("OPENROUTER_API_KEY", "")
        env_content = _read_env(_isolated_janus_home)
        assert "OPENROUTER_API_KEY=" in env_content

    def test_empty_string_routes_to_config(self, _isolated_janus_home):
        """Blanking a config key should write an empty string to config.yaml."""
        set_config_value("model", "")
        config = _read_config(_isolated_janus_home)
        assert "model: ''" in config or "model: \"\"" in config

    def test_zero_routes_to_config(self, _isolated_janus_home):
        """Setting a config key to '0' should write 0 to config.yaml."""
        set_config_value("verbose", "0")
        config = _read_config(_isolated_janus_home)
        assert "verbose: 0" in config

    def test_config_command_rejects_missing_value(self):
        """config set with no value arg (None) should still exit."""
        args = argparse.Namespace(config_command="set", key="model", value=None)
        with pytest.raises(SystemExit):
            config_command(args)

    def test_config_command_accepts_empty_string(self, _isolated_janus_home):
        """config set KEY '' should not exit — it should set the value."""
        args = argparse.Namespace(config_command="set", key="model", value="")
        config_command(args)
        config = _read_config(_isolated_janus_home)
        assert "model" in config


# ---------------------------------------------------------------------------
# List navigation — regression tests for #17876
# ---------------------------------------------------------------------------

class TestListNavigation:
    """janus config set must preserve YAML list fields when using numeric
    indices.  Before #17876, _set_nested would silently replace the entire
    list with a dict, destroying every sibling entry.
    """

    def _write_config(self, tmp_path, body):
        (tmp_path / "config.yaml").write_text(body)

    def test_indexed_set_preserves_sibling_list_entries(self, _isolated_janus_home):
        """Setting custom_providers.0.api_key must not destroy entry 1."""
        self._write_config(_isolated_janus_home, (
            "custom_providers:\n"
            "- name: provider-a\n"
            "  api_key: old-a\n"
            "  base_url: https://a.example.com\n"
            "- name: provider-b\n"
            "  api_key: old-b\n"
            "  base_url: https://b.example.com\n"
        ))

        set_config_value("custom_providers.0.api_key", "new-a")

        import yaml
        reloaded = yaml.safe_load(_read_config(_isolated_janus_home))
        # The list must still be a list
        assert isinstance(reloaded["custom_providers"], list)
        assert len(reloaded["custom_providers"]) == 2
        # Entry 0 was updated
        assert reloaded["custom_providers"][0]["api_key"] == "new-a"
        assert reloaded["custom_providers"][0]["name"] == "provider-a"
        assert reloaded["custom_providers"][0]["base_url"] == "https://a.example.com"
        # Entry 1 is untouched
        assert reloaded["custom_providers"][1]["name"] == "provider-b"
        assert reloaded["custom_providers"][1]["api_key"] == "old-b"
        assert reloaded["custom_providers"][1]["base_url"] == "https://b.example.com"

    def test_indexed_set_preserves_non_targeted_fields(self, _isolated_janus_home):
        """Setting one field in a list entry must not drop other fields."""
        self._write_config(_isolated_janus_home, (
            "custom_providers:\n"
            "- name: provider-a\n"
            "  api_key: old\n"
            "  base_url: https://a.example.com\n"
            "  models:\n"
            "    foo: {}\n"
            "    bar: {}\n"
        ))

        set_config_value("custom_providers.0.api_key", "rotated")

        import yaml
        reloaded = yaml.safe_load(_read_config(_isolated_janus_home))
        entry = reloaded["custom_providers"][0]
        assert entry["api_key"] == "rotated"
        assert entry["name"] == "provider-a"
        assert entry["base_url"] == "https://a.example.com"
        assert set(entry["models"].keys()) == {"foo", "bar"}

    def test_deeper_nesting_through_list(self, _isolated_janus_home):
        """Navigation path mixing dict → list → dict → scalar."""
        self._write_config(_isolated_janus_home, (
            "platforms:\n"
            "  telegram:\n"
            "    allowlist:\n"
            "    - name: alice\n"
            "      role: admin\n"
            "    - name: bob\n"
            "      role: user\n"
        ))

        set_config_value("platforms.telegram.allowlist.1.role", "admin")

        import yaml
        reloaded = yaml.safe_load(_read_config(_isolated_janus_home))
        allowlist = reloaded["platforms"]["telegram"]["allowlist"]
        assert isinstance(allowlist, list)
        assert allowlist[0] == {"name": "alice", "role": "admin"}
        assert allowlist[1] == {"name": "bob", "role": "admin"}


# ---------------------------------------------------------------------------
# A config.yaml that does not parse is never replaced
# ---------------------------------------------------------------------------

_GOOD_CONFIG = (
    "model:\n"
    "  provider: custom\n"
    "  base_url: http://localhost:8000/v1\n"
    "  default: my-model\n"
    "terminal:\n"
    "  timeout: 99\n"
)


class TestBrokenConfigIsNotReplaced:
    """``config set`` on an unparseable config.yaml must write nothing.

    It used to read the file as ``{}`` on a YAML error and write back a
    config holding only the new key, reporting success. The next load then
    refreshed ``config.yaml.last-good`` from that one-key file, so the
    user's model, provider, MCP servers and fallback chain were gone.
    """

    def _break(self, home):
        from janus_cli.config import load_config

        cfg = home / "config.yaml"
        cfg.write_text(_GOOD_CONFIG, encoding="utf-8")
        load_config()  # a good parse keeps config.yaml.last-good
        last_good = home / "config.yaml.last-good"
        assert last_good.is_file()
        with open(cfg, "a", encoding="utf-8") as f:
            f.write("\tskin: classic\n")  # a tab: not valid YAML
        return cfg, last_good, cfg.read_bytes(), last_good.read_bytes()

    def test_set_raises_and_leaves_both_files_alone(self, _isolated_janus_home):
        from janus_cli.config import ConfigParseError, load_config

        cfg, last_good, cfg_bytes, last_good_bytes = self._break(_isolated_janus_home)
        with pytest.raises(ConfigParseError) as exc:
            set_config_value("display.bell", "true")
        assert str(cfg) in str(exc.value)
        assert cfg.read_bytes() == cfg_bytes
        assert last_good.read_bytes() == last_good_bytes
        # The fallback still serves the user's real settings.
        assert load_config()["model"]["default"] == "my-model"

    def test_cli_set_exits_nonzero_without_writing(self, _isolated_janus_home, capsys):
        cfg, _last_good, cfg_bytes, _ = self._break(_isolated_janus_home)
        args = argparse.Namespace(config_command="set", key="display.bell", value="true")
        with pytest.raises(SystemExit) as exc:
            config_command(args)
        assert exc.value.code == 1
        assert cfg.read_bytes() == cfg_bytes
        captured = capsys.readouterr()
        assert "janus config edit" in captured.err
        assert "✓ Set" not in captured.out

    def test_secret_keys_still_go_to_env(self, _isolated_janus_home):
        cfg, _last_good, cfg_bytes, _ = self._break(_isolated_janus_home)
        set_config_value("OPENROUTER_API_KEY", "test-value-123")
        assert "OPENROUTER_API_KEY=test-value-123" in _read_env(_isolated_janus_home)
        assert cfg.read_bytes() == cfg_bytes

    def test_non_mapping_config_is_not_replaced(self, _isolated_janus_home):
        from janus_cli.config import ConfigParseError

        cfg = _isolated_janus_home / "config.yaml"
        cfg.write_text("- just\n- a list\n", encoding="utf-8")
        before = cfg.read_bytes()
        with pytest.raises(ConfigParseError):
            set_config_value("display.bell", "true")
        assert cfg.read_bytes() == before

    def test_empty_config_is_still_writable(self, _isolated_janus_home):
        cfg = _isolated_janus_home / "config.yaml"
        cfg.write_text("# just comments\n", encoding="utf-8")
        set_config_value("display.bell", "true")
        assert "bell: true" in cfg.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# `config set model X` changes the model name, not the provider setup
# ---------------------------------------------------------------------------

_ENDPOINT = "http://127.0.0.1:59999/v1"


class TestModelKeyKeepsProviderSetup:
    """``model:`` is either a bare name or a mapping (default, provider,
    base_url, api_key, ...). Every loader reads a bare name as
    ``{default: name}``. ``config set model X`` used to replace the whole
    mapping with the string, so a custom endpoint's provider, base_url and
    api_key were silently gone and the next run fell back to auto-detection.
    """

    def _write(self, home, body):
        (home / "config.yaml").write_text(body, encoding="utf-8")

    def _model_on_disk(self, home):
        import yaml

        return yaml.safe_load(_read_config(home))["model"]

    def test_set_model_keeps_provider_and_endpoint(self, _isolated_janus_home):
        from janus_cli.runtime_provider import _get_model_config, resolve_requested_provider

        self._write(_isolated_janus_home, (
            "model:\n"
            "  provider: custom\n"
            f"  base_url: {_ENDPOINT}\n"
            "  default: my-model\n"
            "  api_key: k\n"
        ))

        set_config_value("model", "other-model")

        assert self._model_on_disk(_isolated_janus_home) == {
            "provider": "custom",
            "base_url": _ENDPOINT,
            "default": "other-model",
            "api_key": "k",
        }
        runtime_model = _get_model_config()
        assert runtime_model["default"] == "other-model"
        assert runtime_model["base_url"] == _ENDPOINT
        assert resolve_requested_provider() == "custom"

    def test_set_model_reports_the_key_it_wrote(self, _isolated_janus_home, capsys):
        self._write(_isolated_janus_home, (
            "model:\n"
            "  provider: custom\n"
            "  default: my-model\n"
        ))

        set_config_value("model", "other-model")

        out = capsys.readouterr().out
        assert "✓ Set model.default = other-model" in out
        assert "custom" in out  # the kept provider is named, not silent

    def test_set_model_drops_a_stale_model_alias(self, _isolated_janus_home):
        """Some readers prefer ``model.model`` over ``model.default``."""
        self._write(_isolated_janus_home, (
            "model:\n"
            "  provider: openrouter\n"
            "  default: old-model\n"
            "  model: old-model\n"
        ))

        set_config_value("model", "other-model")

        assert self._model_on_disk(_isolated_janus_home) == {
            "provider": "openrouter",
            "default": "other-model",
        }

    def test_model_model_alias_is_not_shadowed_by_default(self, _isolated_janus_home):
        """``config set model.model X`` (multi-profile docs) must take effect.

        Loaders prefer ``default`` over the ``model`` alias, so writing the
        alias next to an existing ``default`` used to change nothing.
        """
        from janus_cli.runtime_provider import _get_model_config

        self._write(_isolated_janus_home, (
            "model:\n"
            "  provider: openrouter\n"
            "  default: old-model\n"
        ))

        set_config_value("model.model", "other-model")

        assert self._model_on_disk(_isolated_janus_home) == {
            "provider": "openrouter",
            "default": "other-model",
        }
        assert _get_model_config()["default"] == "other-model"

    def test_set_model_provider_keeps_a_bare_model_name(self, _isolated_janus_home):
        """The mirror case: a sub-key write must not drop a string model."""
        from janus_cli.runtime_provider import _get_model_config, resolve_requested_provider

        self._write(_isolated_janus_home, "model: gpt-4o\n")

        set_config_value("model.provider", "openrouter")

        assert self._model_on_disk(_isolated_janus_home) == {
            "default": "gpt-4o",
            "provider": "openrouter",
        }
        assert _get_model_config()["default"] == "gpt-4o"
        assert resolve_requested_provider() == "openrouter"

    def test_string_model_is_still_replaced_by_a_string(self, _isolated_janus_home):
        self._write(_isolated_janus_home, "model: old-model\n")

        set_config_value("model", "other-model")

        assert self._model_on_disk(_isolated_janus_home) == "other-model"

    def test_config_without_model_still_gets_a_string(self, _isolated_janus_home, capsys):
        import yaml

        self._write(_isolated_janus_home, "terminal:\n  timeout: 99\n")

        set_config_value("model", "other-model")

        cfg = yaml.safe_load(_read_config(_isolated_janus_home))
        assert cfg == {"terminal": {"timeout": 99}, "model": "other-model"}
        assert "✓ Set model = other-model" in capsys.readouterr().out
