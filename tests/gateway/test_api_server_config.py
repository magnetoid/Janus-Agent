"""Tests for GET/PUT /v1/config on the API server adapter.

The route exists so a host that runs Janus as a service (Blob) can show a
settings page: read the effective configuration, write a change, restart.
Everything here runs against the per-test JANUS_HOME the autouse fixture in
tests/conftest.py provides, so the real ~/.janus is never touched.

The invariant these tests exist to pin: **an API key's value appears in no
response body, ever** — not in GET's provider list, not in PUT's echo.
"""

import json

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_config
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)
from janus_cli.config import DEFAULT_CONFIG, get_config_path, get_env_path


# ---------------------------------------------------------------------------
# Helpers — same shape as tests/gateway/test_api_server.py
# ---------------------------------------------------------------------------


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {}
    if api_key:
        extra["key"] = api_key
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app.router.add_get("/v1/config", adapter._handle_get_config)
    app.router.add_put("/v1/config", adapter._handle_put_config)
    return app


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


def _write_config(text: str) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_config() -> dict:
    return yaml.safe_load(get_config_path().read_text(encoding="utf-8")) or {}


def _no_models(provider_id, *, timeout=10.0):
    """Stand-in for the provider fetch — never touches the network."""
    return None, "patched out"


# ---------------------------------------------------------------------------
# GET /v1/config
# ---------------------------------------------------------------------------


class TestGetConfig:
    @pytest.mark.asyncio
    async def test_no_file_gives_empty_raw_and_default_values(self, adapter, monkeypatch):
        monkeypatch.setattr(api_config, "provider_models", _no_models)
        assert not get_config_path().exists()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/config")
            assert resp.status == 200
            data = await resp.json()

        assert data["object"] == "janus.config"
        assert data["raw"] == ""
        assert data["model"] == {"default": "", "provider": "", "base_url": ""}
        assert data["agent"]["max_turns"] == DEFAULT_CONFIG["agent"]["max_turns"]
        assert data["agent"]["gateway_timeout"] == DEFAULT_CONFIG["agent"]["gateway_timeout"]
        assert data["agent"]["personality"] is None
        assert data["personalities"] == []
        assert data["restart_pending"] is False
        assert isinstance(data["version"], str) and data["version"]
        assert set(data["toolsets"]) == {"available", "enabled"}
        assert data["providers"], "the registry has API-key providers"
        assert all(p["key"]["set"] is False for p in data["providers"])
        assert all(p["env"] and p["id"] and p["name"] for p in data["providers"])

    @pytest.mark.asyncio
    async def test_effective_values_show_even_when_the_file_omits_them(self, adapter, monkeypatch):
        monkeypatch.setattr(api_config, "provider_models", _no_models)
        _write_config("model:\n  default: deepseek-v4-pro\n  provider: deepseek\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            data = await (await cli.get("/v1/config")).json()

        assert data["model"]["default"] == "deepseek-v4-pro"
        assert data["model"]["provider"] == "deepseek"
        # Not in the file, but Janus runs on it.
        assert data["agent"]["max_turns"] == DEFAULT_CONFIG["agent"]["max_turns"]
        assert data["raw"] == "model:\n  default: deepseek-v4-pro\n  provider: deepseek\n"

    @pytest.mark.asyncio
    async def test_a_key_in_env_is_reported_as_set_without_its_value(self, adapter, monkeypatch):
        monkeypatch.setattr(api_config, "provider_models", _no_models)
        get_env_path().write_text("DEEPSEEK_API_KEY=sk-live-secret-a4f2\n", encoding="utf-8")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/config")
            body = await resp.text()
            data = json.loads(body)

        deepseek = next(p for p in data["providers"] if p["id"] == "deepseek")
        assert deepseek["env"] == "DEEPSEEK_API_KEY"
        assert deepseek["key"]["set"] is True
        assert deepseek["key"]["tail"] == "a4f2"
        assert len(deepseek["key"]["tail"]) == 4
        # The invariant: the value is nowhere in the serialised response.
        assert "sk-live-secret-a4f2" not in body
        assert "sk-live" not in body

    @pytest.mark.asyncio
    async def test_personalities_come_from_the_config(self, adapter, monkeypatch):
        monkeypatch.setattr(api_config, "provider_models", _no_models)
        _write_config(
            "personalities:\n"
            "  concise: Be brief.\n"
            "  technical: Be precise.\n"
            "display:\n"
            "  personality: concise\n"
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            data = await (await cli.get("/v1/config")).json()

        assert data["personalities"] == ["concise", "technical"]
        assert data["agent"]["personality"] == "concise"

    @pytest.mark.asyncio
    async def test_models_are_fetched_from_the_provider_and_sorted(self, adapter, monkeypatch):
        _write_config("model:\n  provider: deepseek\n  default: deepseek-v4-pro\n")
        calls = []

        def fake(provider_id, *, timeout=10.0):
            calls.append(provider_id)
            return ["deepseek-v4-pro", "deepseek-flash"], None

        monkeypatch.setattr(api_config, "provider_models", fake)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            data = await (await cli.get("/v1/config")).json()

        assert calls == ["deepseek"]
        assert data["models"]["provider"] == "deepseek"
        assert data["models"]["ids"] == ["deepseek-v4-pro", "deepseek-flash"]
        assert data["models"]["reason"] is None

    @pytest.mark.asyncio
    async def test_a_failed_model_fetch_gives_null_ids_and_one_line(self, adapter, monkeypatch):
        _write_config("model:\n  provider: deepseek\n")

        def fake(provider_id, *, timeout=10.0):
            return None, "ReadTimeout: timed out"

        monkeypatch.setattr(api_config, "provider_models", fake)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            data = await (await cli.get("/v1/config")).json()

        assert data["models"]["ids"] is None
        assert data["models"]["reason"] == "ReadTimeout: timed out"
        assert "\n" not in data["models"]["reason"]

    @pytest.mark.asyncio
    async def test_no_provider_configured_does_not_fetch(self, adapter, monkeypatch):
        calls = []

        def fake(provider_id, *, timeout=10.0):
            calls.append(provider_id)
            return [], None

        monkeypatch.setattr(api_config, "provider_models", fake)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            data = await (await cli.get("/v1/config")).json()

        assert calls == []
        assert data["models"]["provider"] is None
        assert data["models"]["ids"] is None
        assert data["models"]["reason"]

    @pytest.mark.asyncio
    async def test_an_oauth_provider_is_not_fetched(self, adapter, monkeypatch):
        _write_config("model:\n  provider: nous\n")
        calls = []

        def fake(provider_id, *, timeout=10.0):
            calls.append(provider_id)
            return [], None

        monkeypatch.setattr(api_config, "provider_models", fake)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            data = await (await cli.get("/v1/config")).json()

        assert calls == []
        assert data["models"]["ids"] is None
        assert "api-key" in data["models"]["reason"] or "API-key" in data["models"]["reason"]

    @pytest.mark.asyncio
    async def test_toolsets_report_available_and_enabled(self, adapter, monkeypatch):
        monkeypatch.setattr(api_config, "provider_models", _no_models)
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            data = await (await cli.get("/v1/config")).json()

        available = data["toolsets"]["available"]
        enabled = data["toolsets"]["enabled"]
        assert isinstance(available, list) and available
        assert isinstance(enabled, list)
        assert all(isinstance(name, str) for name in available + enabled)
        assert available == sorted(available)
        assert enabled == sorted(enabled)


# ---------------------------------------------------------------------------
# PUT /v1/config — the merge path
# ---------------------------------------------------------------------------


class TestPutMerge:
    @pytest.mark.asyncio
    async def test_a_model_change_leaves_every_other_key_untouched(self, adapter):
        _write_config(
            "model:\n"
            "  default: old-model\n"
            "  provider: deepseek\n"
            "agent:\n"
            "  max_turns: 42\n"
            "timezone: Europe/Belgrade\n"
        )
        before = _read_config()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}, "restart": False})
            assert resp.status == 200
            data = await resp.json()

        after = _read_config()
        assert data["applied"]["model"] == {"default": "x"}
        assert after["model"]["default"] == "x"
        # Every other key survived the rewrite.
        assert after["model"]["provider"] == before["model"]["provider"]
        assert after["agent"] == before["agent"]
        assert after["timezone"] == before["timezone"]
        assert set(after) == set(before)

    @pytest.mark.asyncio
    async def test_the_merge_never_writes_defaults_into_the_users_file(self, adapter):
        _write_config("model:\n  default: old-model\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"agent": {"max_turns": 7}, "restart": False})
            assert resp.status == 200

        after = _read_config()
        assert after == {"model": {"default": "old-model"}, "agent": {"max_turns": 7}}

    @pytest.mark.asyncio
    async def test_a_merge_into_a_missing_file_creates_it(self, adapter):
        assert not get_config_path().exists()

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config", json={"model": {"default": "m", "provider": "deepseek"}, "restart": False}
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["applied"]["model"] == {"default": "m", "provider": "deepseek"}
        assert _read_config() == {"model": {"default": "m", "provider": "deepseek"}}

    @pytest.mark.asyncio
    async def test_toolsets_are_set_whole(self, adapter):
        _write_config("toolsets:\n  - janus-cli\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"toolsets": ["janus-cli", "web"], "restart": False})
            assert resp.status == 200
            data = await resp.json()

        assert data["applied"]["toolsets"] == ["janus-cli", "web"]
        assert _read_config()["toolsets"] == ["janus-cli", "web"]

    @pytest.mark.asyncio
    async def test_the_personality_goes_to_the_key_the_config_uses(self, adapter):
        _write_config("personalities:\n  concise: Be brief.\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"agent": {"personality": "concise"}, "restart": False})
            assert resp.status == 200
            data = await resp.json()

        assert data["applied"]["agent"] == {"personality": "concise"}
        assert _read_config()["display"]["personality"] == "concise"

    @pytest.mark.asyncio
    async def test_a_null_value_is_skipped(self, adapter):
        _write_config("model:\n  default: keep-me\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config",
                json={"model": {"default": None, "provider": "deepseek"}, "restart": False},
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["applied"]["model"] == {"provider": "deepseek"}
        assert _read_config()["model"] == {"default": "keep-me", "provider": "deepseek"}

    @pytest.mark.asyncio
    async def test_a_merge_that_would_break_the_file_is_refused(self, adapter):
        # fallback_model as a list of non-dicts → an error-severity issue.
        _write_config("fallback_model:\n  - nonsense\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}, "restart": False})
            assert resp.status == 400
            data = await resp.json()

        assert data["error"] == "invalid config"
        assert any(issue["severity"] == "error" for issue in data["issues"])
        assert _read_config() == {"fallback_model": ["nonsense"]}


# ---------------------------------------------------------------------------
# PUT /v1/config — the raw path
# ---------------------------------------------------------------------------


class TestPutRaw:
    @pytest.mark.asyncio
    async def test_raw_replaces_the_file(self, adapter):
        _write_config("model:\n  default: old\nagent:\n  max_turns: 42\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config", json={"raw": "model:\n  default: new\n", "restart": False}
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["applied"]["raw"] is True
        assert _read_config() == {"model": {"default": "new"}}

    @pytest.mark.asyncio
    async def test_a_scalar_is_refused(self, adapter):
        _write_config("model:\n  default: old\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"raw": "just a string", "restart": False})
            assert resp.status == 400
            data = await resp.json()

        assert "mapping" in data["error"]
        assert _read_config() == {"model": {"default": "old"}}

    @pytest.mark.asyncio
    async def test_unparseable_yaml_is_refused(self, adapter):
        _write_config("model:\n  default: old\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"raw": "model:\n  - [unclosed\n", "restart": False})
            assert resp.status == 400
            await resp.json()

        assert _read_config() == {"model": {"default": "old"}}

    @pytest.mark.asyncio
    async def test_a_structure_error_is_refused_with_the_issue(self, adapter):
        _write_config("model:\n  default: old\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config",
                json={"raw": "custom_providers:\n  base_url: https://example.test/v1\n", "restart": False},
            )
            assert resp.status == 400
            data = await resp.json()

        assert data["error"] == "invalid config"
        errors = [i for i in data["issues"] if i["severity"] == "error"]
        assert errors and "custom_providers" in errors[0]["message"]
        assert errors[0]["hint"]
        assert _read_config() == {"model": {"default": "old"}}

    @pytest.mark.asyncio
    async def test_a_warning_only_file_is_written_and_the_warning_returned(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config",
                json={"raw": "fallback_model:\n  model: some-model\n", "restart": False},
            )
            assert resp.status == 200
            data = await resp.json()

        assert _read_config() == {"fallback_model": {"model": "some-model"}}
        assert data["warnings"], "the warning is reported, not swallowed"
        assert any("provider" in w["message"] for w in data["warnings"])
        assert all(w["severity"] == "warning" for w in data["warnings"])


# ---------------------------------------------------------------------------
# PUT /v1/config — refusals
# ---------------------------------------------------------------------------


class TestPutRefusals:
    @pytest.mark.asyncio
    async def test_raw_with_model_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config",
                json={"raw": "model:\n  default: x\n", "model": {"default": "y"}, "restart": False},
            )
            assert resp.status == 400
            data = await resp.json()

        assert "raw" in data["error"]
        assert not get_config_path().exists()

    @pytest.mark.asyncio
    async def test_an_empty_body_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={})
            assert resp.status == 400
            data = await resp.json()

        assert data["error"] == "empty body"

    @pytest.mark.asyncio
    async def test_invalid_json_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400
            data = await resp.json()

        assert data["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_an_unknown_agent_key_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config", json={"agent": {"max_turns": 5, "nonsense": 1}, "restart": False}
            )
            assert resp.status == 400
            data = await resp.json()

        assert "nonsense" in data["error"]
        assert not get_config_path().exists()

    @pytest.mark.asyncio
    async def test_an_unknown_model_key_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"api_key": "sk-nope"}, "restart": False})
            assert resp.status == 400
            data = await resp.json()

        assert "api_key" in data["error"]
        assert not get_config_path().exists()

    @pytest.mark.asyncio
    async def test_a_number_sent_as_a_string_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"agent": {"max_turns": "60"}, "restart": False})
            assert resp.status == 400
            data = await resp.json()

        assert "agent.max_turns" in data["error"]
        assert not get_config_path().exists()

    @pytest.mark.asyncio
    async def test_a_negative_timeout_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config", json={"agent": {"gateway_timeout": -1}, "restart": False}
            )
            assert resp.status == 400
            assert not get_config_path().exists()

    @pytest.mark.asyncio
    async def test_a_non_string_model_default_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": 7}, "restart": False})
            assert resp.status == 400
            data = await resp.json()

        assert "model.default" in data["error"]
        assert not get_config_path().exists()

    @pytest.mark.asyncio
    async def test_a_non_list_toolsets_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"toolsets": "janus-cli", "restart": False})
            assert resp.status == 400
            assert not get_config_path().exists()


# ---------------------------------------------------------------------------
# PUT /v1/config — api_keys
# ---------------------------------------------------------------------------


class TestPutApiKeys:
    @pytest.mark.asyncio
    async def test_a_key_lands_in_env_and_never_in_the_response(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config",
                json={"api_keys": {"DEEPSEEK_API_KEY": "sk-live-secret-a4f2"}, "restart": False},
            )
            assert resp.status == 200
            body = await resp.text()
            data = json.loads(body)

        assert data["applied"]["api_keys"] == ["DEEPSEEK_API_KEY"]
        assert "sk-live-secret-a4f2" not in body
        assert "a4f2" not in body
        env_text = get_env_path().read_text(encoding="utf-8")
        assert "DEEPSEEK_API_KEY=sk-live-secret-a4f2" in env_text

    @pytest.mark.asyncio
    async def test_an_empty_value_removes_the_key(self, adapter):
        get_env_path().write_text("DEEPSEEK_API_KEY=sk-old\nOTHER=keep\n", encoding="utf-8")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config", json={"api_keys": {"DEEPSEEK_API_KEY": ""}, "restart": False}
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["applied"]["api_keys"] == ["DEEPSEEK_API_KEY"]
        env_text = get_env_path().read_text(encoding="utf-8")
        assert "DEEPSEEK_API_KEY" not in env_text
        assert "OTHER=keep" in env_text

    @pytest.mark.asyncio
    async def test_a_name_outside_the_pattern_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"api_keys": {"PATH": "/tmp"}, "restart": False})
            assert resp.status == 400
            data = await resp.json()

        assert "PATH" in data["error"]
        assert not get_env_path().exists()

    @pytest.mark.asyncio
    async def test_a_lowercase_name_is_refused(self, adapter):
        # The pattern is the outer gate; _reject_denylisted_env_var runs behind
        # it (no denylisted name — PATH, PYTHONPATH, JANUS_HOME — can match it).
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put(
                "/v1/config", json={"api_keys": {"lowercase_api_key": "x"}, "restart": False}
            )
            assert resp.status == 400
            data = await resp.json()

        assert "lowercase_api_key" in data["error"]

    @pytest.mark.asyncio
    async def test_a_non_string_value_is_refused(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"api_keys": {"X_API_KEY": 42}, "restart": False})
            assert resp.status == 400
            assert not get_env_path().exists()


# ---------------------------------------------------------------------------
# PUT /v1/config — the restart
# ---------------------------------------------------------------------------


class TestPutRestart:
    @pytest.mark.asyncio
    async def test_the_handler_is_called_once_and_restarting_is_true(self, adapter):
        calls = []
        adapter.set_restart_handler(lambda: calls.append(1) or True)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}})
            assert resp.status == 200
            data = await resp.json()

        assert calls == [1]
        assert data["restarting"] is True
        assert data["warnings"] == []
        assert data["drain_timeout_seconds"] > 0
        assert adapter._restart_pending is True

    @pytest.mark.asyncio
    async def test_no_handler_reports_not_restarting_with_a_warning(self, adapter):
        assert adapter._restart_handler is None

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}})
            assert resp.status == 200
            data = await resp.json()

        assert data["restarting"] is False
        assert any("restart" in w["message"] for w in data["warnings"])
        assert adapter._restart_pending is False
        # The change is still on disk.
        assert _read_config()["model"]["default"] == "x"

    @pytest.mark.asyncio
    async def test_restart_false_does_not_call_the_handler(self, adapter):
        calls = []
        adapter.set_restart_handler(lambda: calls.append(1) or True)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}, "restart": False})
            assert resp.status == 200
            data = await resp.json()

        assert calls == []
        assert data["restarting"] is False
        assert adapter._restart_pending is False

    @pytest.mark.asyncio
    async def test_restart_false_as_a_string_does_not_call_the_handler(self, adapter):
        """Some clients serialise JSON booleans as strings; "false" must mean false."""
        calls = []
        adapter.set_restart_handler(lambda: calls.append(1) or True)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}, "restart": "false"})
            assert resp.status == 200
            data = await resp.json()

        assert calls == []
        assert data["restarting"] is False

    @pytest.mark.asyncio
    async def test_a_refused_restart_reports_false(self, adapter):
        adapter.set_restart_handler(lambda: False)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}})
            data = await resp.json()

        assert data["restarting"] is False
        assert adapter._restart_pending is False

    @pytest.mark.asyncio
    async def test_restart_pending_shows_in_the_next_get(self, adapter, monkeypatch):
        monkeypatch.setattr(api_config, "provider_models", _no_models)
        adapter.set_restart_handler(lambda: True)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            assert (await (await cli.get("/v1/config")).json())["restart_pending"] is False
            await cli.put("/v1/config", json={"model": {"default": "x"}})
            data = await (await cli.get("/v1/config")).json()

        assert data["restart_pending"] is True
        assert data["raw"] == "model:\n  default: x\n"
        assert data["model"]["default"] == "x"


# ---------------------------------------------------------------------------
# Auth and managed installs
# ---------------------------------------------------------------------------


class TestGuards:
    @pytest.mark.asyncio
    async def test_get_requires_the_bearer(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            assert (await cli.get("/v1/config")).status == 401
            resp = await cli.get("/v1/config", headers={"Authorization": "Bearer nope"})
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_put_requires_the_bearer(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.put("/v1/config", json={"model": {"default": "x"}})
            assert resp.status == 401
        assert not get_config_path().exists()

    @pytest.mark.asyncio
    async def test_the_bearer_admits(self, auth_adapter, monkeypatch):
        monkeypatch.setattr(api_config, "provider_models", _no_models)
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/config", headers={"Authorization": "Bearer sk-secret"})
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_managed_install_refuses_both(self, adapter, monkeypatch):
        monkeypatch.setattr(api_config, "is_managed", lambda: True)
        _write_config("model:\n  default: untouched\n")

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            get_resp = await cli.get("/v1/config")
            put_resp = await cli.put("/v1/config", json={"model": {"default": "x"}})

            assert get_resp.status == 409
            assert put_resp.status == 409
            assert "managed" in (await put_resp.json())["error"]

        assert _read_config() == {"model": {"default": "untouched"}}


# ---------------------------------------------------------------------------
# api_config internals
# ---------------------------------------------------------------------------


class TestProviderModels:
    def test_a_failed_fetch_returns_one_line_and_no_key(self, monkeypatch):
        get_env_path().write_text("DEEPSEEK_API_KEY=sk-live-secret-a4f2\n", encoding="utf-8")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-secret-a4f2")

        class Boom(Exception):
            def __str__(self):
                return "connect failed for sk-live-secret-a4f2\nsecond line"

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                raise Boom()

        monkeypatch.setattr("httpx.Client", FakeClient)
        ids, reason = api_config.provider_models("deepseek")

        assert ids is None
        assert "sk-live-secret-a4f2" not in reason
        assert "\n" not in reason

    def test_ids_are_sorted_and_deduped(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}, {"no": "id"}, "junk"]}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                assert url == "https://api.deepseek.com/v1/models"
                assert headers["Authorization"] == "Bearer sk-test"
                return FakeResponse()

        monkeypatch.setattr("httpx.Client", FakeClient)
        ids, reason = api_config.provider_models("deepseek")

        assert ids == ["a", "b"]
        assert reason is None

    def test_a_missing_key_is_reported_without_a_request(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("no request should be made without a key")

        monkeypatch.setattr("httpx.Client", boom)
        ids, reason = api_config.provider_models("deepseek")

        assert ids is None
        assert "key" in reason
