"""Training-data tier: a one-line privacy signal shown when a model is chosen."""

from types import SimpleNamespace

import pytest
import yaml

from janus_cli import model_switch as ms


class TestTierTable:
    def test_every_classified_provider_is_a_real_provider(self):
        from janus_cli.models import CANONICAL_PROVIDERS

        known = {p.slug for p in CANONICAL_PROVIDERS}
        assert set(ms._TRAINING_TIERS) <= known

    def test_every_tier_has_a_note(self):
        assert set(ms._TRAINING_TIERS.values()) <= set(ms._TIER_NOTES)
        assert all(note.strip() for note in ms._TIER_NOTES.values())

    def test_unclassified_provider_says_so_instead_of_staying_silent(self):
        tier, note = ms.training_data_tier("some-new-provider")

        assert tier == "unknown"
        assert note == ms._TIER_NOTES["unknown"]

    def test_aliases_resolve_to_the_canonical_provider(self):
        assert ms.training_data_tier("Anthropic")[0] == ms.training_data_tier("anthropic")[0]

    @pytest.mark.parametrize(
        "base_url",
        ["http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:8080/v1"],
    )
    def test_loopback_endpoint_is_local_whatever_the_provider(self, base_url):
        assert ms.training_data_tier("custom", base_url)[0] == "local"
        assert ms.training_data_tier("openrouter", base_url)[0] == "local"

    def test_remote_custom_endpoint_is_not_called_local(self):
        assert ms.training_data_tier("custom", "https://llm.example.com/v1")[0] != "local"

    def test_line_carries_the_note(self):
        line = ms.training_data_line("custom", "http://localhost:11434/v1")

        assert line.startswith("Training data: ")
        assert ms._TIER_NOTES["local"] in line


class TestCliModelSwitch:
    def test_switch_confirmation_shows_the_training_line(self, monkeypatch):
        import cli as cli_mod
        from janus_cli.model_switch import ModelSwitchResult
        from tests.cli.test_cli_model_picker_comforts import _make_cli

        printed = []
        monkeypatch.setattr(cli_mod, "_cprint", lambda text="", *a, **k: printed.append(str(text)))
        monkeypatch.setattr(cli_mod, "save_config_value", lambda k, v: True)
        monkeypatch.setattr(
            "janus_cli.model_switch.switch_model",
            lambda **kw: ModelSwitchResult(
                success=True, new_model="llama3", target_provider="custom",
                provider_changed=True, api_key="", base_url="http://localhost:11434/v1", api_mode="",
            ),
        )
        monkeypatch.setattr("janus_cli.model_switch.resolve_display_context_length", lambda *a, **k: None)
        agent = type("Agent", (), {"reasoning_config": None, "switch_model": lambda self, **kw: None})()
        cli_obj = _make_cli(
            agent=agent, provider="anthropic", requested_provider="anthropic",
            base_url="", api_key="", api_mode="", reasoning_config=None,
            _explicit_api_key="", _explicit_base_url="",
        )

        cli_obj._handle_model_switch("/model llama3")

        assert any(ms._TIER_NOTES["local"] in line for line in printed)


class TestGatewayModelSwitch:
    @pytest.mark.asyncio
    async def test_switch_reply_shows_the_training_line(self, tmp_path, monkeypatch):
        import gateway.runner as gateway_runner
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource
        from janus_cli.model_switch import ModelSwitchResult

        janus_home = tmp_path / ".janus"
        janus_home.mkdir()
        (janus_home / "config.yaml").write_text(
            yaml.safe_dump({"model": {"default": "old"}}), encoding="utf-8"
        )
        monkeypatch.setattr(gateway_runner, "_janus_home", janus_home)
        monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
        monkeypatch.setattr(
            "janus_cli.model_switch.switch_model",
            lambda **kw: ModelSwitchResult(
                success=True, new_model="deepseek-chat", target_provider="deepseek",
                provider_changed=True, api_key="sk-test", base_url="https://api.deepseek.com/v1",
                api_mode="chat_completions", provider_label="DeepSeek",
            ),
        )
        monkeypatch.setattr("janus_constants.get_janus_home", lambda: janus_home)
        monkeypatch.setattr("janus_cli.config.get_janus_home", lambda: janus_home)

        runner = object.__new__(GatewayRunner)
        runner.adapters = {}
        runner._voice_mode = {}
        runner._session_model_overrides = {}
        runner._session_reasoning_overrides = {}
        runner._running_agents = {}
        event = MessageEvent(
            text="/model deepseek-chat",
            message_type=MessageType.TEXT,
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        )

        reply = await runner._handle_model_command(event)

        assert ms.training_data_tier("deepseek")[1] in reply


class TestJanusModelCommand:
    def _run(self, monkeypatch, pick):
        from janus_cli import main as main_mod
        from janus_constants import get_janus_home

        cfg = get_janus_home() / "config.yaml"
        cfg.write_text(yaml.safe_dump({"model": {"default": "old", "provider": "anthropic"}}),
                       encoding="utf-8")
        monkeypatch.setattr(main_mod, "_require_tty", lambda name: None)

        def _select(args=None):
            if pick:
                cfg.write_text(yaml.safe_dump({"model": pick}), encoding="utf-8")

        monkeypatch.setattr(main_mod, "select_provider_and_model", _select)
        main_mod.cmd_model(SimpleNamespace(local=False, refresh=False))

    def test_new_pick_prints_the_training_line(self, monkeypatch, capsys):
        self._run(monkeypatch, {"default": "llama3", "provider": "custom",
                                "base_url": "http://localhost:11434/v1"})

        assert ms._TIER_NOTES["local"] in capsys.readouterr().out

    def test_cancelled_pick_prints_nothing(self, monkeypatch, capsys):
        self._run(monkeypatch, None)

        assert "Training data" not in capsys.readouterr().out
