"""``on_failure: warn|block`` across plugin, shell and gateway hooks.

``warn`` (the default) keeps the old behaviour: a failing hook is logged and
skipped. ``block`` makes a failing decision hook veto what it guards.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent import shell_hooks
from gateway.hooks import HookRegistry
from janus_cli.plugins import PluginManager


def _plugin(janus_home: Path, name: str, register_body: str, **manifest_extra) -> None:
    plugin_dir = janus_home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    manifest = {"name": name, "version": "0.1.0", **manifest_extra}
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(f"def register(ctx):\n    {register_body}\n", encoding="utf-8")
    cfg_path = janus_home / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg.setdefault("plugins", {}).setdefault("enabled", []).append(name)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


@pytest.fixture()
def janus_home(tmp_path, monkeypatch):
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setenv("JANUS_HOME", str(home))
    return home


def _manager() -> PluginManager:
    mgr = PluginManager()
    mgr.discover_and_load()
    return mgr


RAISING_PRE_TOOL = 'ctx.register_hook("pre_tool_call", lambda **kw: 1/0)'


class TestPluginHooks:
    def test_failing_pre_tool_call_blocks_under_block(self, janus_home):
        _plugin(janus_home, "guard", RAISING_PRE_TOOL, on_failure="block")
        results = _manager().invoke_hook("pre_tool_call", tool_name="terminal", args={})
        assert len(results) == 1
        assert results[0]["action"] == "block"
        assert "guard" in results[0]["message"]

    def test_failing_pre_tool_call_is_skipped_by_default(self, janus_home):
        _plugin(janus_home, "guard", RAISING_PRE_TOOL)
        assert _manager().invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []

    def test_block_policy_leaves_working_hooks_alone(self, janus_home):
        _plugin(janus_home, "guard", 'ctx.register_hook("pre_tool_call", lambda **kw: None)', on_failure="block")
        assert _manager().invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []

    def test_per_hook_mapping(self, janus_home):
        _plugin(
            janus_home,
            "guard",
            'ctx.register_hook("pre_tool_call", lambda **kw: 1/0); '
            'ctx.register_hook("pre_gateway_dispatch", lambda **kw: 1/0)',
            on_failure={"pre_gateway_dispatch": "block"},
        )
        mgr = _manager()
        assert mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
        [skip] = mgr.invoke_hook("pre_gateway_dispatch", event=object())
        assert skip["action"] == "skip"
        assert "guard" in skip["reason"]

    def test_invalid_policy_falls_back_to_warn(self, janus_home):
        _plugin(janus_home, "guard", RAISING_PRE_TOOL, on_failure="explode")
        assert _manager().invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []

    def test_observer_hooks_are_never_blocked(self, janus_home):
        _plugin(janus_home, "guard", 'ctx.register_hook("post_tool_call", lambda **kw: 1/0)', on_failure="block")
        assert _manager().invoke_hook("post_tool_call", tool_name="terminal", args={}) == []

    def test_plugin_that_fails_to_load_blocks_its_declared_hooks(self, janus_home):
        _plugin(janus_home, "guard", "raise RuntimeError('boom')", on_failure="block", provides_hooks=["pre_tool_call"])
        [blocked] = _manager().invoke_hook("pre_tool_call", tool_name="terminal", args={})
        assert blocked["action"] == "block"
        assert "guard" in blocked["message"]

    def test_plugin_that_fails_to_load_blocks_nothing_by_default(self, janus_home):
        _plugin(janus_home, "guard", "raise RuntimeError('boom')", provides_hooks=["pre_tool_call"])
        assert _manager().invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "hook.sh"
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")
class TestShellHooks:
    def test_entry_parses_on_failure(self):
        spec = shell_hooks._parse_single_entry("pre_tool_call", 0, {"command": "x", "on_failure": "block"})
        assert spec.on_failure == "block"
        assert shell_hooks._parse_single_entry("pre_tool_call", 0, {"command": "x"}).on_failure == "warn"
        assert shell_hooks._parse_single_entry("pre_tool_call", 0, {"command": "x", "on_failure": 3}).on_failure == "warn"

    def test_non_zero_exit_blocks_under_block(self, tmp_path):
        spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=_script(tmp_path, "exit 2"), on_failure="block")
        result = shell_hooks._make_callback(spec)(tool_name="terminal", args={})
        assert result["action"] == "block"
        assert "exit" in result["message"]

    def test_missing_command_blocks_under_block(self, tmp_path):
        spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=str(tmp_path / "nope.sh"), on_failure="block")
        assert shell_hooks._make_callback(spec)(tool_name="terminal", args={})["action"] == "block"

    def test_non_zero_exit_is_skipped_by_default(self, tmp_path):
        spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=_script(tmp_path, "exit 2"))
        assert shell_hooks._make_callback(spec)(tool_name="terminal", args={}) is None

    def test_success_is_not_a_failure(self, tmp_path):
        spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=_script(tmp_path, "exit 0"), on_failure="block")
        assert shell_hooks._make_callback(spec)(tool_name="terminal", args={}) is None

    def test_explicit_block_keeps_the_hooks_message(self, tmp_path):
        command = _script(tmp_path, "echo '{\"action\": \"block\", \"message\": \"no rm\"}'; exit 1")
        spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=command, on_failure="block")
        assert shell_hooks._make_callback(spec)(tool_name="terminal", args={}) == {"action": "block", "message": "no rm"}


def _gateway_hook(hooks_dir: Path, name: str, handler: str, **manifest_extra) -> None:
    hook_dir = hooks_dir / name
    hook_dir.mkdir(parents=True)
    manifest = {"name": name, "events": ["command:reset", "agent:start"], **manifest_extra}
    (hook_dir / "HOOK.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (hook_dir / "handler.py").write_text(handler, encoding="utf-8")


def _registry(hooks_dir: Path) -> HookRegistry:
    reg = HookRegistry()
    with patch("gateway.hooks.HOOKS_DIR", hooks_dir):
        reg.discover_and_load()
    return reg


RAISING_HANDLER = "def handle(event_type, context):\n    raise RuntimeError('boom')\n"


class TestGatewayHooks:
    @pytest.mark.asyncio
    async def test_failing_command_hook_denies_under_block(self, tmp_path):
        _gateway_hook(tmp_path, "guard", RAISING_HANDLER, on_failure="block")
        [denied] = await _registry(tmp_path).emit_collect("command:reset", {})
        assert denied["decision"] == "deny"
        assert "guard" in denied["message"]

    @pytest.mark.asyncio
    async def test_failing_async_command_hook_denies_under_block(self, tmp_path):
        handler = "async def handle(event_type, context):\n    raise RuntimeError('boom')\n"
        _gateway_hook(tmp_path, "guard", handler, on_failure="block")
        [denied] = await _registry(tmp_path).emit_collect("command:reset", {})
        assert denied["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_failing_command_hook_is_skipped_by_default(self, tmp_path):
        _gateway_hook(tmp_path, "guard", RAISING_HANDLER)
        assert await _registry(tmp_path).emit_collect("command:reset", {}) == []

    @pytest.mark.asyncio
    async def test_non_command_events_are_never_blocked(self, tmp_path):
        _gateway_hook(tmp_path, "guard", RAISING_HANDLER, on_failure="block")
        reg = _registry(tmp_path)
        assert await reg.emit_collect("agent:start", {}) == []
        await reg.emit("agent:start", {})

    @pytest.mark.asyncio
    async def test_hook_that_fails_to_load_denies_under_block(self, tmp_path):
        _gateway_hook(tmp_path, "guard", "def handle(:\n", on_failure="block")
        [denied] = await _registry(tmp_path).emit_collect("command:reset", {})
        assert denied["decision"] == "deny"
        assert "guard" in denied["message"]

    @pytest.mark.asyncio
    async def test_hook_that_fails_to_load_is_skipped_by_default(self, tmp_path):
        _gateway_hook(tmp_path, "guard", "def handle(:\n")
        assert await _registry(tmp_path).emit_collect("command:reset", {}) == []

    @pytest.mark.asyncio
    async def test_missing_handler_denies_under_block(self, tmp_path):
        _gateway_hook(tmp_path, "guard", "", on_failure="block")
        (tmp_path / "guard" / "handler.py").unlink()
        [denied] = await _registry(tmp_path).emit_collect("command:reset", {})
        assert denied["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_handler_without_handle_denies_under_block(self, tmp_path):
        _gateway_hook(tmp_path, "guard", "x = 1\n", on_failure="block")
        [denied] = await _registry(tmp_path).emit_collect("command:reset", {})
        assert denied["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_missing_handler_is_skipped_by_default(self, tmp_path):
        _gateway_hook(tmp_path, "guard", "")
        (tmp_path / "guard" / "handler.py").unlink()
        reg = _registry(tmp_path)
        assert await reg.emit_collect("command:reset", {}) == []
        assert reg.loaded_hooks == []
