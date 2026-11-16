"""Tests for janus_cli.tool_availability — direct-credential tool probes."""

import pytest

from janus_cli import tool_availability as ta


@pytest.fixture
def env(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(ta, "get_env_value", lambda key: values.get(key, ""))
    monkeypatch.setattr(ta, "has_agent_browser", lambda: True)
    monkeypatch.setattr(ta, "local_browser_runnable", lambda: True)
    return values


def test_camofox_url_wins_over_explicit_cloud_provider(env):
    env["CAMOFOX_URL"] = "http://localhost:9377"
    env["BROWSERBASE_API_KEY"] = "bb"
    env["BROWSERBASE_PROJECT_ID"] = "proj"
    label, available = ta.browser_state({"browser": {"cloud_provider": "browserbase"}})
    assert (label, available) == (ta.browser_label("camofox"), True)


def test_explicit_cloud_provider_requires_its_own_credentials(env):
    env["BROWSER_USE_API_KEY"] = "bu"
    label, available = ta.browser_state({"browser": {"cloud_provider": "browserbase"}})
    assert label == ta.browser_label("browserbase")
    assert available is False

    env["BROWSERBASE_API_KEY"] = "bb"
    env["BROWSERBASE_PROJECT_ID"] = "proj"
    assert ta.browser_state({"browser": {"cloud_provider": "browserbase"}}) == (
        ta.browser_label("browserbase"),
        True,
    )


def test_cloud_provider_needs_agent_browser_cli(env, monkeypatch):
    env["BROWSERBASE_API_KEY"] = "bb"
    env["BROWSERBASE_PROJECT_ID"] = "proj"
    monkeypatch.setattr(ta, "has_agent_browser", lambda: False)
    _, available = ta.browser_state({"browser": {"cloud_provider": "browserbase"}})
    assert available is False


def test_auto_detects_cloud_credentials_without_explicit_provider(env):
    env["BROWSER_USE_API_KEY"] = "bu"
    assert ta.browser_state({}) == (ta.browser_label("browser-use"), True)


def test_falls_back_to_local_browser_probe(env, monkeypatch):
    monkeypatch.setattr(ta, "local_browser_runnable", lambda: False)
    assert ta.browser_state({}) == (ta.browser_label("local"), False)


def test_web_state_reports_configured_backend_and_key_presence(env):
    backend, available = ta.web_state({"web": {"backend": "Tavily"}})
    assert backend == "tavily"
    assert available is False

    env["TAVILY_API_KEY"] = "tv"
    assert ta.web_state({"web": {"backend": "tavily"}}) == ("tavily", True)
