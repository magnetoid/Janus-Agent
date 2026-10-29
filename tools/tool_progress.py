"""Mid-call progress from long-running tools (plan item 3.6).

The tool executor binds the running agent for the current turn
(:func:`bind_agent`); ContextVars follow the turn into worker threads via
``tools.thread_context.propagate_context_to_thread``. A tool that learns
about progress — today MCP ``notifications/progress`` — calls
:func:`report_progress`, which forwards a ``tool.progress`` event to the
agent's ``tool_progress_callback`` (CLI spinner text, gateway progress
relays) and touches the agent's activity timer so a slow but live tool is
not mistaken for a hung one.

Never raises; with nothing bound it is a no-op.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_bound: contextvars.ContextVar[Optional[Callable[..., None]]] = contextvars.ContextVar(
    "janus_tool_progress_sink", default=None)


def bind_agent(agent: Any) -> contextvars.Token:
    """Route progress reports in this context (and threads it spawns) to *agent*."""
    def _sink(tool_name: str, message: str, progress: Optional[float], total: Optional[float]) -> None:
        cb = getattr(agent, "tool_progress_callback", None)
        if cb:
            cb("tool.progress", tool_name, message, None, progress=progress, total=total)
        touch = getattr(agent, "_touch_activity", None)
        if callable(touch):
            touch(f"{tool_name}: {message}" if message else f"{tool_name} in progress")
    return _bound.set(_sink)


def unbind() -> None:
    _bound.set(None)


def current_sink() -> Optional[Callable[..., None]]:
    """The sink bound to this context — capture it before hopping to another loop/thread."""
    return _bound.get()


def report_progress(tool_name: str, message: str = "", progress: Optional[float] = None,
                    total: Optional[float] = None, *, sink: Optional[Callable[..., None]] = None) -> None:
    target = sink or _bound.get()
    if target is None:
        return
    try:
        target(tool_name, message or "", progress, total)
    except Exception as exc:
        logger.debug("tool progress report failed: %s", exc)
