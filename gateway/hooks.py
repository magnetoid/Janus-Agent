"""
Event Hook System

A lightweight event-driven system that fires handlers at key lifecycle points.
Hooks are discovered from ~/.janus/hooks/ directories, each containing:
  - HOOK.yaml  (metadata: name, description, events list)
  - handler.py (Python handler with async def handle(event_type, context))

Events:
  - gateway:startup     -- Gateway process starts
  - session:start       -- New session created (first message of a new session)
  - session:end         -- Session ends (user ran /new or /reset)
  - session:reset       -- Session reset completed (new session entry created)
  - agent:start         -- Agent begins processing a message
  - agent:step          -- Each turn in the tool-calling loop
  - agent:end           -- Agent finishes processing
  - command:*           -- Any slash command executed (wildcard match)

Errors in hooks are caught and logged but never block the main pipeline,
unless HOOK.yaml sets ``on_failure: block``: then a ``command:*`` hook that
raises, or fails to load, denies the command instead of being skipped.
"""

import asyncio
import importlib.util
import sys
from typing import Any, Callable, Dict, List, Optional

import yaml

from janus_cli.config import get_janus_home
from janus_cli.plugins import parse_failure_policy


HOOKS_DIR = get_janus_home() / "hooks"


def _deny(hook_name: str, reason: str) -> Dict[str, str]:
    return {
        "decision": "deny",
        "message": f"Command blocked: gateway hook '{hook_name}' failed ({reason}) and is set to on_failure: block.",
    }


def _fail_closed(hook_name: str, handle_fn: Callable) -> Callable:
    """Wrap *handle_fn* so an exception on a ``command:*`` event denies it."""

    async def _guarded(event_type: str, context: Dict[str, Any]) -> Any:
        try:
            result = handle_fn(event_type, context)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as e:
            print(f"[hooks] Error in handler for '{event_type}': {e}", flush=True)
            if event_type.startswith("command:"):
                return _deny(hook_name, f"{type(e).__name__}: {e}")
            return None

    return _guarded


class HookRegistry:
    """
    Discovers, loads, and fires event hooks.

    Usage:
        registry = HookRegistry()
        registry.discover_and_load()
        await registry.emit("agent:start", {"platform": "telegram", ...})
    """

    def __init__(self):
        # event_type -> [handler_fn, ...]
        self._handlers: Dict[str, List[Callable]] = {}
        self._loaded_hooks: List[dict] = []  # metadata for listing

    @property
    def loaded_hooks(self) -> List[dict]:
        """Return metadata about all loaded hooks."""
        return list(self._loaded_hooks)

    def _register_builtin_hooks(self) -> None:
        """Register built-in hooks that are always active.

        Currently empty — no shipped built-in hooks. Kept as the extension
        point for future always-on gateway hooks so they drop in without
        re-plumbing discover_and_load().
        """
        return

    def discover_and_load(self) -> None:
        """
        Scan the hooks directory for hook directories and load their handlers.

        Also registers built-in hooks that are always active.

        Each hook directory must contain:
          - HOOK.yaml with at least 'name' and 'events' keys
          - handler.py with a top-level 'handle' function (sync or async)
        """
        self._register_builtin_hooks()

        if not HOOKS_DIR.exists():
            return

        for hook_dir in sorted(HOOKS_DIR.iterdir()):
            if not hook_dir.is_dir():
                continue

            manifest_path = hook_dir / "HOOK.yaml"
            handler_path = hook_dir / "handler.py"

            if not manifest_path.exists() or not handler_path.exists():
                continue

            hook_name, events, policies = hook_dir.name, [], {}
            try:
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                if not manifest or not isinstance(manifest, dict):
                    print(f"[hooks] Skipping {hook_dir.name}: invalid HOOK.yaml", flush=True)
                    continue

                hook_name = manifest.get("name", hook_dir.name)
                events = manifest.get("events", [])
                policies = parse_failure_policy(manifest.get("on_failure"), f"[hooks] {hook_name}")
                if not events:
                    print(f"[hooks] Skipping {hook_name}: no events declared", flush=True)
                    continue

                # Dynamically load the handler module.
                # Register in sys.modules BEFORE exec_module so Pydantic /
                # dataclasses / typing introspection can resolve forward
                # references (triggered by `from __future__ import annotations`
                # in the handler). Without this, a handler that declares a
                # Pydantic BaseModel for webhook/event payloads fails at first
                # dispatch with "TypeAdapter ... is not fully defined".
                module_name = f"janus_hook_{hook_name}"
                spec = importlib.util.spec_from_file_location(
                    module_name, handler_path
                )
                if spec is None or spec.loader is None:
                    print(f"[hooks] Skipping {hook_name}: could not load handler.py", flush=True)
                    continue

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

                handle_fn = getattr(module, "handle", None)
                if handle_fn is None:
                    print(f"[hooks] Skipping {hook_name}: no 'handle' function found", flush=True)
                    continue

                # Register the handler for each declared event
                for event in events:
                    fn = handle_fn
                    if self._blocks(policies, event):
                        fn = _fail_closed(hook_name, handle_fn)
                    self._handlers.setdefault(event, []).append(fn)

                self._loaded_hooks.append({
                    "name": hook_name,
                    "description": manifest.get("description", ""),
                    "events": events,
                    "path": str(hook_dir),
                })

                print(f"[hooks] Loaded hook '{hook_name}' for events: {events}", flush=True)

            except Exception as e:
                print(f"[hooks] Error loading hook {hook_dir.name}: {e}", flush=True)
                self._register_load_failure_denials(hook_name, events, policies, e)

    @staticmethod
    def _blocks(policies: Dict[str, str], event: str) -> bool:
        return str(event).startswith("command:") and policies.get(event, policies.get("*")) == "block"

    def _register_load_failure_denials(
        self, hook_name: str, events: Any, policies: Dict[str, str], error: Exception
    ) -> None:
        """A broken ``on_failure: block`` hook denies the commands it guards."""
        if not isinstance(events, list):
            return
        denial = _deny(hook_name, f"did not load: {error}")
        for event in events:
            if not self._blocks(policies, event):
                continue

            def _denied(event_type: str, context: Dict[str, Any]) -> Dict[str, str]:
                return dict(denial)

            self._handlers.setdefault(event, []).append(_denied)
            print(f"[hooks] {hook_name} failed to load; {event} is denied (on_failure: block)", flush=True)

    def _resolve_handlers(self, event_type: str) -> List[Callable]:
        """Return all handlers that should fire for ``event_type``.

        Exact matches fire first, followed by wildcard matches (e.g.
        ``command:*`` matches ``command:reset``).
        """
        handlers = list(self._handlers.get(event_type, []))
        if ":" in event_type:
            base = event_type.split(":")[0]
            wildcard_key = f"{base}:*"
            handlers.extend(self._handlers.get(wildcard_key, []))
        return handlers

    async def emit(self, event_type: str, context: Optional[Dict[str, Any]] = None) -> None:
        """
        Fire all handlers registered for an event, discarding return values.

        Supports wildcard matching: handlers registered for "command:*" will
        fire for any "command:..." event. Handlers registered for a base type
        like "agent" won't fire for "agent:start" -- only exact matches and
        explicit wildcards.

        Args:
            event_type: The event identifier (e.g. "agent:start").
            context:    Optional dict with event-specific data.
        """
        if context is None:
            context = {}

        for fn in self._resolve_handlers(event_type):
            try:
                result = fn(event_type, context)
                # Support both sync and async handlers
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                print(f"[hooks] Error in handler for '{event_type}': {e}", flush=True)

    async def emit_collect(
        self,
        event_type: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """Fire handlers and return their non-None return values in order.

        Like :meth:`emit` but captures each handler's return value. Used for
        decision-style hooks (e.g. ``command:<name>`` policies that want to
        allow/deny/rewrite the command before normal dispatch).

        Exceptions from individual handlers are logged but do not abort the
        remaining handlers.
        """
        if context is None:
            context = {}

        results: List[Any] = []
        for fn in self._resolve_handlers(event_type):
            try:
                result = fn(event_type, context)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    results.append(result)
            except Exception as e:
                print(f"[hooks] Error in handler for '{event_type}': {e}", flush=True)
        return results
