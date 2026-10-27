"""Resolve which terminal backend UNATTENDED runs should use.

Interactive sessions keep ``terminal.backend`` (default ``local``) — switching
them to a container breaks host-cwd expectations. Unattended surfaces (cron
jobs, kanban workers, TTY-less one-shots) have no human watching, so they get a
sandbox when one is available. OpenClaw shipped host execution for everything by
default and paid for it; this is the minimal, reversible alternative.

``terminal.headless_backend``:
  * ``"auto"``     — Docker when a usable daemon is present, else no override
  * ``"inherit"``  — never override; behave exactly as before
  * ``"<name>"``   — an explicit backend (docker, ssh, modal, daytona, singularity, local)

Returns ``None`` whenever nothing should be overridden, so every caller's
previous behaviour is byte-for-byte preserved when the key is unset/inherit.
Pure and import-light: no janus_cli imports (callers pass the loaded config).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

_KNOWN_BACKENDS = frozenset({"local", "docker", "ssh", "modal", "daytona", "singularity"})
_NO_OVERRIDE = frozenset({"", "inherit", "none"})


def headless_backend_setting(config: Optional[Mapping[str, Any]]) -> str:
    """The normalized ``terminal.headless_backend`` value (``"auto"`` default)."""
    try:
        terminal = (config or {}).get("terminal") or {}
        raw = terminal.get("headless_backend", "auto") if isinstance(terminal, Mapping) else "auto"
        if raw is None:
            return "auto"
        # NOTE: an explicit "" is kept as "" — it means "no override" (see _NO_OVERRIDE),
        # only a missing/None key falls back to the "auto" default.
        return str(raw).strip().lower()
    except Exception:
        return "auto"


def resolve_headless_backend(config: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Backend name to force for an unattended run, or ``None`` to leave it alone."""
    setting = headless_backend_setting(config)
    if setting in _NO_OVERRIDE:
        return None
    if setting == "auto":
        try:
            from tools.environments.docker import docker_usable
            return "docker" if docker_usable() else None
        except Exception:
            return None
    if setting in _KNOWN_BACKENDS:
        return setting
    return None
