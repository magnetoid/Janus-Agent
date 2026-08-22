"""Tests for /personality none — clearing personality overlay."""
from contextlib import contextmanager

import pytest
from unittest.mock import MagicMock, patch
import yaml


@contextmanager
def _gateway_janus_home(tmp_path):
    """Point the gateway's janus home at ``tmp_path`` for the duration.

    ``_janus_home`` is bound at import time in ``gateway.core`` and re-bound
    into ``gateway.runner`` (``from gateway.core import _janus_home``), which is
    where the /personality handler and ``_load_gateway_config()`` read it from.
    ``gateway.run`` is only a back-compat shim that *copies* private names into
    its own namespace, so patching ``gateway.run._janus_home`` patches a dead
    copy and the handler keeps reading the process-wide home — the config the
    test wrote is never seen.
    """
    import gateway.core
    import gateway.runner

    with patch.object(gateway.core, "_janus_home", tmp_path), \
         patch.object(gateway.runner, "_janus_home", tmp_path):
        yield


def _write_config(tmp_path, config_data):
    (tmp_path / "config.yaml").write_text(yaml.dump(config_data))


def _read_config(tmp_path):
    return yaml.safe_load((tmp_path / "config.yaml").read_text()) or {}


# ── CLI tests ──────────────────────────────────────────────────────────────

class TestCLIPersonalityNone:

    def _make_cli(self, personalities=None):
        from cli import JanusCLI
        cli = JanusCLI.__new__(JanusCLI)
        cli.personalities = personalities or {
            "helpful": "You are helpful.",
            "concise": "You are concise.",
        }
        cli.system_prompt = "You are kawaii~"
        cli.agent = MagicMock()
        cli.console = MagicMock()
        return cli

    def test_none_clears_system_prompt(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality none")
        assert cli.system_prompt == ""

    def test_default_clears_system_prompt(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality default")
        assert cli.system_prompt == ""

    def test_neutral_clears_system_prompt(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality neutral")
        assert cli.system_prompt == ""

    def test_none_forces_agent_reinit(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality none")
        assert cli.agent is None

    def test_none_saves_to_config(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True) as mock_save:
            cli._handle_personality_command("/personality none")
        mock_save.assert_called_once_with("agent.system_prompt", "")

    def test_known_personality_still_works(self):
        cli = self._make_cli()
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality helpful")
        assert cli.system_prompt == "You are helpful."

    def test_unknown_personality_shows_none_in_available(self, capsys):
        cli = self._make_cli()
        cli._handle_personality_command("/personality nonexistent")
        output = capsys.readouterr().out
        assert "none" in output.lower()

    def test_list_shows_none_option(self):
        cli = self._make_cli()
        with patch("builtins.print") as mock_print:
            cli._handle_personality_command("/personality")
        output = " ".join(str(c) for c in mock_print.call_args_list)
        assert "none" in output.lower()


# ── Gateway tests ──────────────────────────────────────────────────────────

class TestGatewayPersonalityNone:

    def _make_event(self, args=""):
        event = MagicMock()
        event.get_command.return_value = "personality"
        event.get_command_args.return_value = args
        return event

    def _make_runner(self, personalities=None):
        from gateway.run import GatewayRunner
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._ephemeral_system_prompt = "You are kawaii~"
        runner.config = {
            "agent": {
                "personalities": personalities or {"helpful": "You are helpful."}
            }
        }
        return runner

    @pytest.mark.asyncio
    @pytest.mark.parametrize("clear_word", ["none", "default", "neutral"])
    async def test_clearing_word_clears_prompt_and_persists(self, tmp_path, clear_word):
        runner = self._make_runner()
        _write_config(tmp_path, {
            "agent": {
                "personalities": {"helpful": "You are helpful."},
                "system_prompt": "You are kawaii~",
            }
        })

        with _gateway_janus_home(tmp_path):
            result = await runner._handle_personality_command(self._make_event(clear_word))

        # In-memory overlay is dropped for the next message …
        assert runner._ephemeral_system_prompt == ""
        # … and the change is persisted, so a gateway restart stays cleared.
        assert _read_config(tmp_path)["agent"]["system_prompt"] == ""
        assert "cleared" in result.lower()

    @pytest.mark.asyncio
    async def test_known_personality_sets_and_persists_prompt(self, tmp_path):
        """The counterpart of clearing: a named personality becomes the overlay."""
        runner = self._make_runner()
        _write_config(tmp_path, {"agent": {"personalities": {"helpful": "You are helpful."}}})

        with _gateway_janus_home(tmp_path):
            result = await runner._handle_personality_command(self._make_event("helpful"))

        assert runner._ephemeral_system_prompt == "You are helpful."
        assert _read_config(tmp_path)["agent"]["system_prompt"] == "You are helpful."
        assert "helpful" in result

    @pytest.mark.asyncio
    async def test_list_includes_none(self, tmp_path):
        runner = self._make_runner()
        _write_config(tmp_path, {"agent": {"personalities": {"helpful": "You are helpful."}}})

        with _gateway_janus_home(tmp_path):
            result = await runner._handle_personality_command(self._make_event(""))

        # Anchor on the configured personality first: that proves we are looking
        # at the real listing and not an error/fallback string that happens to
        # contain "none" (a tmp-path name did exactly that before).
        assert "helpful" in result
        assert "none" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_shows_none_in_available(self, tmp_path):
        runner = self._make_runner()
        _write_config(tmp_path, {"agent": {"personalities": {"helpful": "You are helpful."}}})

        with _gateway_janus_home(tmp_path):
            # deliberately free of the substring "none" so the assertion below
            # can only be satisfied by the offered clearing option
            result = await runner._handle_personality_command(self._make_event("bogus-persona"))

        assert "bogus-persona" in result
        assert "helpful" in result
        assert "none" in result.lower()
        # An unknown name must not silently change the overlay.
        assert runner._ephemeral_system_prompt == "You are kawaii~"

    @pytest.mark.asyncio
    async def test_empty_personality_list_uses_profile_display_path(self, tmp_path):
        runner = self._make_runner(personalities={})
        _write_config(tmp_path, {"agent": {"personalities": {}}})

        with _gateway_janus_home(tmp_path), \
             patch("janus_constants.display_janus_home", return_value="~/.janus/profiles/coder"):
            result = await runner._handle_personality_command(self._make_event(""))

        # The message must point at the *profile's* config, not a hardcoded ~/.janus.
        assert "~/.janus/profiles/coder/config.yaml" in result


class TestPersonalityDictFormat:
    """Test dict-format custom personalities with description, tone, style."""

    def _make_cli(self, personalities):
        from cli import JanusCLI
        cli = JanusCLI.__new__(JanusCLI)
        cli.personalities = personalities
        cli.system_prompt = ""
        cli.agent = None
        cli.console = MagicMock()
        return cli

    def test_dict_personality_uses_system_prompt(self):
        cli = self._make_cli({
            "coder": {
                "description": "Expert programmer",
                "system_prompt": "You are an expert programmer.",
                "tone": "technical",
                "style": "concise",
            }
        })
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality coder")
        assert "You are an expert programmer." in cli.system_prompt

    def test_dict_personality_includes_tone(self):
        cli = self._make_cli({
            "coder": {
                "system_prompt": "You are an expert programmer.",
                "tone": "technical and precise",
            }
        })
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality coder")
        assert "Tone: technical and precise" in cli.system_prompt

    def test_dict_personality_includes_style(self):
        cli = self._make_cli({
            "coder": {
                "system_prompt": "You are an expert programmer.",
                "style": "use code examples",
            }
        })
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality coder")
        assert "Style: use code examples" in cli.system_prompt

    def test_string_personality_still_works(self):
        cli = self._make_cli({"helper": "You are helpful."})
        with patch("cli.save_config_value", return_value=True):
            cli._handle_personality_command("/personality helper")
        assert cli.system_prompt == "You are helpful."

    def test_resolve_prompt_dict_no_tone_no_style(self):
        from cli import JanusCLI
        result = JanusCLI._resolve_personality_prompt({
            "description": "A helper",
            "system_prompt": "You are helpful.",
        })
        assert result == "You are helpful."

    def test_resolve_prompt_string(self):
        from cli import JanusCLI
        result = JanusCLI._resolve_personality_prompt("You are helpful.")
        assert result == "You are helpful."
