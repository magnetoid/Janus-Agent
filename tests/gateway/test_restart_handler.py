"""Tests for the restart handler an adapter can ask for.

Task 2 (config-route-for-blob) extracts the /restart command's environment
decision (systemd service vs. detached re-exec) out of
``_handle_restart_command`` into ``GatewayRunner._request_gateway_restart``,
and gives every ``BasePlatformAdapter`` a ``set_restart_handler`` hook beside
``set_message_handler`` so a caller other than the /restart command — the
PUT /v1/config route Task 3 adds — can ask for the same graceful restart.
Nothing in this task *uses* that handler yet.
"""

import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)


def _patch_exists_present(monkeypatch, present_path: str) -> None:
    """Make ``os.path.exists`` answer True for one path, real for everything else."""
    real_exists = os.path.exists

    def fake_exists(path):
        if str(path) == present_path:
            return True
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)


def _patch_exists_absent(monkeypatch) -> None:
    """Make ``os.path.exists`` answer False for both container markers.

    Without this, a test run *inside* a container (this project's own CI
    image build boots one) would see ``/.dockerenv`` for real and the
    "detached" test would fail for a reason that has nothing to do with the
    code under test.
    """
    real_exists = os.path.exists
    blocked = {"/.dockerenv", "/run/.containerenv"}

    def fake_exists(path):
        if str(path) in blocked:
            return False
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)


# ── GatewayRunner._request_gateway_restart ──────────────────────────────


def test_request_gateway_restart_uses_service_restart_under_systemd(monkeypatch):
    """systemd (INVOCATION_ID set) restarts via the service manager."""
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    _patch_exists_absent(monkeypatch)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    result = runner._request_gateway_restart()

    assert result is True
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


def test_request_gateway_restart_uses_service_restart_in_docker_container(monkeypatch):
    """No systemd, but /.dockerenv exists — still the service-restart path."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_exists_present(monkeypatch, "/.dockerenv")
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    result = runner._request_gateway_restart()

    assert result is True
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


def test_request_gateway_restart_uses_service_restart_in_podman_container(monkeypatch):
    """Podman marks a container with /run/.containerenv, not /.dockerenv."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_exists_present(monkeypatch, "/run/.containerenv")
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    result = runner._request_gateway_restart()

    assert result is True
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


def test_request_gateway_restart_uses_detached_restart_otherwise(monkeypatch):
    """Neither systemd nor a container — the detached re-exec path."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_exists_absent(monkeypatch)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    result = runner._request_gateway_restart()

    assert result is True
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


def test_request_gateway_restart_returns_false_when_already_under_way(monkeypatch):
    """The return value passes through request_restart's idempotency signal."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_exists_absent(monkeypatch)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=False)

    result = runner._request_gateway_restart()

    assert result is False
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


# ── _handle_restart_command delegates to _request_gateway_restart ───────


@pytest.mark.asyncio
async def test_handle_restart_command_calls_request_gateway_restart_once(gateway_home):
    """/restart still triggers exactly one restart request, through the new seam."""
    runner, _adapter = make_restart_runner()
    runner._request_gateway_restart = MagicMock(return_value=True)

    source = make_restart_source(chat_id="42")
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    result = await runner._handle_restart_command(event)

    runner._request_gateway_restart.assert_called_once_with()
    assert "Restarting" in result


# ── start() wires set_restart_handler onto every adapter ────────────────


@pytest.mark.asyncio
async def test_start_wires_restart_handler_onto_adapter(monkeypatch, tmp_path):
    """start() calls adapter.set_restart_handler(self._request_gateway_restart)
    at the same point it calls set_message_handler (gateway/runner.py:2775).

    make_restart_runner() builds its GatewayRunner via object.__new__, which
    skips __init__ — enough for the handler-level tests above, but start()
    itself reads real __init__ state (e.g. self._busy_text_mode) that fixture
    never sets. tests/gateway/test_platform_reconnect.py's heavier start()
    tests show what driving it through an object.__new__ runner actually
    costs: half a dozen extra patches (discover_plugins, load_config,
    build_channel_directory, process-registry recovery, a faked
    asyncio.create_task...) to keep unrelated startup machinery from running
    or crashing. A real GatewayRunner(config) gets all of that for free —
    tests/gateway/test_runner_fatal_adapter.py already drives start() this
    way with nothing patched but _create_adapter — so that construction is
    used here too; RestartTestAdapter (the same stub the tests above use) is
    still the adapter.
    """
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")},
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    adapter = RestartTestAdapter()
    monkeypatch.setattr(runner, "_create_adapter", lambda platform, platform_config: adapter)

    ok = await runner.start()

    assert ok is True
    # Bound methods compare equal by (__self__, __func__), not identity — two
    # attribute reads of the same bound method are equal but not `is`.
    assert adapter._restart_handler == runner._request_gateway_restart


# ── the reconnect path wires it too ─────────────────────────────────────


def _make_reconnect_runner() -> GatewayRunner:
    """A GatewayRunner with only what ``_platform_reconnect_watcher`` touches.

    ``make_restart_runner`` builds the state the /restart tests need, which is
    a different set — no ``_failed_platforms``, no ``adapters``. This is the
    recipe ``tests/gateway/test_platform_reconnect.py::_make_runner`` uses,
    which is the fixture that already drives this watcher successfully.
    """
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._exit_with_failure = False
    runner._exit_cleanly = False
    runner._failed_platforms = {}
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._honcho_managers = {}
    runner._honcho_configs = {}
    runner._shutdown_all_gateway_honcho = lambda: None
    runner.session_store = MagicMock()
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_platform_reconnect_wires_restart_handler_onto_adapter():
    """The second wiring site (gateway/runner.py ~4528).

    A platform that failed at startup and came back later gets a *new* adapter
    from ``_create_adapter``; without the handler set there too, /v1/config on
    a reconnected API server would report "no gateway to restart" forever.
    """
    runner = _make_reconnect_runner()
    runner._failed_platforms[Platform.TELEGRAM] = {
        "config": PlatformConfig(enabled=True, token="test"),
        "attempts": 1,
        "next_retry": time.monotonic() - 1,  # due now
    }
    adapter = RestartTestAdapter()
    real_sleep = asyncio.sleep

    calls = 0

    async def fake_sleep(_seconds):
        # The first sleep is the watcher's startup delay; stop it after the
        # pass that follows, the way test_platform_reconnect.py drives it.
        nonlocal calls
        calls += 1
        if calls > 1:
            runner._running = False
        await real_sleep(0)

    with patch.object(runner, "_create_adapter", return_value=adapter):
        with patch("gateway.run.build_channel_directory", create=True):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await runner._platform_reconnect_watcher()

    assert runner.adapters[Platform.TELEGRAM] is adapter
    # Bound methods compare equal by (__self__, __func__), not identity.
    assert adapter._restart_handler == runner._request_gateway_restart


# ── BasePlatformAdapter.set_restart_handler / _restart_handler ──────────


def test_restart_handler_defaults_to_none():
    """An adapter with nobody having called set_restart_handler reports none."""
    adapter = RestartTestAdapter()

    assert adapter._restart_handler is None


def test_set_restart_handler_installs_callable():
    handler = MagicMock(return_value=True)
    adapter = RestartTestAdapter()

    adapter.set_restart_handler(handler)

    assert adapter._restart_handler is handler
    assert adapter._restart_handler() is True
    handler.assert_called_once_with()
