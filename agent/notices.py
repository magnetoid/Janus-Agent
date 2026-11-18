"""Out-of-band agent notices (driver-agnostic payload).

The agent fires these via ``AIAgent._emit_notice`` → ``notice_callback`` (and
clears them via ``_emit_notice_clear`` → ``notice_clear_callback``); each
driver renders them its own way — the TUI as a status-bar override, the CLI as
a console line queued until the turn ends, messaging platforms as a one-shot
plaintext push (``gateway.core.render_notice_line``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentNotice:
    """A structured, driver-agnostic out-of-band notice."""

    text: str
    level: str = "info"            # info | warn | error | success
    kind: str = "sticky"           # sticky | ttl
    ttl_ms: Optional[int] = None   # honored only when kind == "ttl"
    key: Optional[str] = None      # dedupe / clear key
    id: Optional[str] = None
