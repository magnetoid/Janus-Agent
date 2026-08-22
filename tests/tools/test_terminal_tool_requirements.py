"""Tests for terminal/file tool availability in local dev environments."""

import importlib

import pytest

from model_tools import get_tool_definitions

terminal_tool_module = importlib.import_module("tools.terminal_tool")

_tools_discovered = False


@pytest.fixture(autouse=True)
def _registered_tools_and_clear_caches():
    """Register the built-in tools, then invalidate the check_fn and
    tool-definitions caches so monkeypatched env vars / config take effect.

    ``discover_builtin_tools()`` has to be called explicitly: importing
    ``model_tools`` used to run it as a module-level side effect, but that was
    removed in the gateway core/runner refactor. Every real entry point
    (``cli.py``, ``janus_cli/main.py``, ``gateway/runner.py``) calls it before
    asking for tool definitions, so the test has to do the same — otherwise
    the registry only contains whatever this module happened to import
    directly (``tools.terminal_tool``) and every other tool looks "unavailable"
    for reasons that have nothing to do with backend requirements.
    """
    from tools.registry import discover_builtin_tools, invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    global _tools_discovered
    if not _tools_discovered:
        # Runs inside a fixture (not at module import) so the conftest
        # hermetic-env fixture has already redirected JANUS_HOME. Discovery
        # re-globs and re-reads every tools/*.py on each call, so do it once
        # per file rather than once per test.
        discover_builtin_tools()
        _tools_discovered = True
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    yield
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()


class TestTerminalRequirements:
    def test_local_backend_requirements(self, monkeypatch):
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {"env_type": "local"},
        )
        assert terminal_tool_module.check_terminal_requirements() is True

    def test_terminal_and_file_tools_resolve_for_local_backend(self, monkeypatch):
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {"env_type": "local"},
        )
        tools = get_tool_definitions(enabled_toolsets=["terminal", "file"], quiet_mode=True)
        names = {tool["function"]["name"] for tool in tools}
        assert "terminal" in names
        assert {"read_file", "write_file", "patch", "search_files"}.issubset(names)

    def test_terminal_and_execute_code_tools_resolve_for_managed_modal(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.tool_backend_helpers.managed_nous_tools_enabled", lambda: True)
        monkeypatch.setattr(terminal_tool_module, "managed_nous_tools_enabled", lambda: True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {"env_type": "modal", "modal_mode": "managed"},
        )
        monkeypatch.setattr(
            terminal_tool_module,
            "is_managed_tool_gateway_ready",
            lambda _vendor: True,
        )
        tools = get_tool_definitions(enabled_toolsets=["terminal", "code_execution"], quiet_mode=True)
        names = {tool["function"]["name"] for tool in tools}

        assert "terminal" in names
        assert "execute_code" in names
