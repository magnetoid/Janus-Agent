"""Tests for the restart handler an adapter can ask for.

Task 2 (config-route-for-blob) extracts the /restart command's environment
decision (systemd service vs. detached re-exec) out of
``_handle_restart_command`` into ``GatewayRunner._request_gateway_restart``,
and gives every ``BasePlatformAdapter`` a ``set_restart_handler`` hook beside
``set_message_handler`` so a caller other than the /restart command — the
PUT /v1/config route Task 3 adds — can ask for the same graceful restart.
Nothing in this task *uses* that handler yet.
"""

import os
from unittest.mock import MagicMock

import pytest

from gateway.platforms.base import MessageEvent, MessageType
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
