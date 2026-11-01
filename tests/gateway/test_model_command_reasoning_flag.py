"""``/model <name> --reasoning <level>`` in gateway chats."""

from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._running_agents = {}
    return runner


def _make_event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import gateway.runner as gateway_runner
    from janus_cli.model_switch import ModelSwitchResult

    janus_home = tmp_path / ".janus"
    janus_home.mkdir()
    cfg_path = janus_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": {"default": "old"}}), encoding="utf-8")

    monkeypatch.setattr(gateway_runner, "_janus_home", janus_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "janus_cli.model_switch.switch_model",
        lambda **kw: ModelSwitchResult(
            success=True,
            new_model="gpt-5.5",
            target_provider="openrouter",
            provider_changed=True,
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            provider_label="OpenRouter",
        ),
    )
    monkeypatch.setattr("janus_constants.get_janus_home", lambda: janus_home)
    monkeypatch.setattr("janus_cli.config.get_janus_home", lambda: janus_home)
    return cfg_path


@pytest.mark.asyncio
async def test_switch_with_reasoning_sets_session_override(isolated):
    runner = _make_runner()
    event = _make_event("/model gpt-5.5 --reasoning high")

    reply = await runner._handle_model_command(event)

    assert "gpt-5.5" in reply and "high" in reply
    key = runner._session_key_for_source(event.source)
    assert runner._session_reasoning_overrides[key] == {"enabled": True, "effort": "high"}
    # Session-only: nothing about reasoning written to config.yaml.
    assert "agent" not in (yaml.safe_load(isolated.read_text(encoding="utf-8")) or {})


@pytest.mark.asyncio
async def test_global_switch_persists_reasoning(isolated):
    runner = _make_runner()

    await runner._handle_model_command(_make_event("/model gpt-5.5 --reasoning none --global"))

    written = yaml.safe_load(isolated.read_text(encoding="utf-8"))
    assert written["agent"]["reasoning_effort"] == "none"
    assert written["model"]["default"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_reasoning_alone_changes_effort_without_switching(isolated, monkeypatch):
    monkeypatch.setattr(
        "janus_cli.model_switch.switch_model",
        lambda **kw: pytest.fail("no model switch expected"),
    )
    runner = _make_runner()
    event = _make_event("/model --reasoning low")
    key = runner._session_key_for_source(event.source)
    live_agent = SimpleNamespace(reasoning_config=None)
    import threading
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {key: (live_agent, "sig")}

    reply = await runner._handle_model_command(event)

    assert "low" in reply
    assert live_agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert runner._agent_cache[key][1] == "sig"


@pytest.mark.asyncio
async def test_unknown_level_is_refused(isolated, monkeypatch):
    monkeypatch.setattr(
        "janus_cli.model_switch.switch_model",
        lambda **kw: pytest.fail("no model switch expected"),
    )
    reply = await _make_runner()._handle_model_command(_make_event("/model gpt-5.5 --reasoning loud"))

    assert "xhigh" in reply
