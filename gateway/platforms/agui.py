"""AG-UI protocol helpers for the API server.

`AG-UI <https://docs.ag-ui.com>`_ is an open, event-based protocol for agents talking to
user-facing applications. Speaking it means a chat platform can call Janus directly and
render the answer as an ordinary message, with no platform-specific adapter on either
side. The first caller is Blob, which POSTs a ``RunAgentInput`` and reads back an SSE
event stream.

Everything here is a pure function of dicts and bytes: no aiohttp, no agent, no I/O. The
handler in ``api_server.py`` supplies all of those. That split is what lets the protocol
rules — signature verification, message parsing, frame encoding — be tested without a
server or a model.

Two details in the AG-UI catalogue cost more to rediscover than to write down:

* **Wire ``type`` values are SCREAMING_SNAKE.** The published docs head each section with
  the TypeScript interface name (``TextMessageStart``); the discriminator on the wire is
  ``TEXT_MESSAGE_START``. Emitting the heading form parses as nothing, silently.
* **Field names are camelCase**, in every SDK, including the Python one — it declares
  ``message_id`` and serialises ``messageId``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: Refuse a body larger than this before parsing it.
AGUI_MAX_BODY_BYTES = 1_000_000

#: How far the caller's timestamp may be from ours. Matches Blob's own window; it is what
#: stops a captured request being replayed later with a fresh header.
AGUI_TIMESTAMP_TOLERANCE_SECONDS = 300

#: Hard stop for one turn. Deliberately inside the caller's cap — Blob gives up at 120s,
#: and a truncated answer we chose to send beats a timeout it had to invent.
DEFAULT_RUN_TIMEOUT_SECONDS = 100.0

#: Comment frames while a turn is thinking, so no proxy between here and the caller
#: decides the connection is idle and closes it.
DEFAULT_KEEPALIVE_SECONDS = 15.0

#: Channel context is untrusted text. It informs the turn; it does not get to be large.
MAX_CONTEXT_PROMPT_BYTES = 8_192

_SESSION_KEY_SAFE = re.compile(r"[^A-Za-z0-9._:-]")


def positive_float(raw: Optional[str], fallback: float) -> float:
    """A duration from the environment, or the default if it is not one.

    Deliberately forgiving. This is read while the adapter is being constructed, so
    raising here would stop the whole gateway starting — every platform, not just this
    route — and "BLOB_AGUI_KEEPALIVE_SECONDS=15s" is exactly the kind of typo that
    deserves a default rather than an outage.
    """
    try:
        value = float(raw) if raw is not None and raw != "" else fallback
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


class AGUISignatureError(Exception):
    """A request whose signature did not check out.

    Carries a stable machine-readable ``reason`` for logs, while the response says only
    that authentication failed — a caller must not be able to learn *which* check
    rejected it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify_blob_signature(
    *,
    raw_body: bytes,
    timestamp: Optional[str],
    signature: Optional[str],
    secret: str,
    now: Optional[float] = None,
    tolerance: int = AGUI_TIMESTAMP_TOLERANCE_SECONDS,
) -> str:
    """Check Blob's ``v0=`` HMAC over the raw body. Returns the digest, or raises.

    The scheme is Slack's, which Blob adopted deliberately: ``v0:{timestamp}:{body}``
    signed with HMAC-SHA256. The timestamp is inside the signed string, so a captured
    request cannot be replayed with a fresh header.

    It must run against the *raw* bytes, before any JSON parse — re-serialising a parsed
    body changes whitespace and key order, and the digest with it.
    """
    if not secret:
        raise AGUISignatureError("no_secret_configured")
    if not timestamp or not signature:
        raise AGUISignatureError("missing_headers")
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        raise AGUISignatureError("bad_timestamp") from None
    if abs((time.time() if now is None else now) - sent_at) > tolerance:
        raise AGUISignatureError("stale_timestamp")

    base = f"v0:{sent_at}:".encode() + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise AGUISignatureError("bad_signature")
    return expected


@dataclass(frozen=True)
class RunInput:
    """One AG-UI run, reduced to what a Janus turn needs."""

    thread_id: str
    run_id: str
    user_message: str
    #: Everything before the trailing user turn, oldest first, in OpenAI shape.
    history: List[Dict[str, str]]
    context_prompt: Optional[str]


def parse_run_input(body: Dict[str, Any]) -> RunInput:
    """Turn a ``RunAgentInput`` into a Janus turn. Raises ValueError if it cannot.

    The caller owns the transcript and sends all of it every time, so there is no session
    to look up and nothing to persist: the last user message is the prompt, everything
    before it is history.
    """
    if not isinstance(body, dict):
        raise ValueError("body must be an object")

    thread_id = body.get("threadId")
    run_id = body.get("runId")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("threadId is required")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("runId is required")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("messages must be an array")

    history: List[Dict[str, str]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        # Anything that is not the agent's own past turn is somebody talking to it. The
        # speaker's name is folded into the text because the OpenAI message shape the
        # agent consumes has nowhere else to put it, and in a group conversation "who
        # said this" is not decoration.
        if item.get("role") == "assistant":
            history.append({"role": "assistant", "content": content})
        else:
            name = item.get("name")
            prefix = f"{name}: " if isinstance(name, str) and name else ""
            history.append({"role": "user", "content": f"{prefix}{content}"})

    if not history:
        raise ValueError("messages contained nothing to answer")

    last = history.pop()
    if last["role"] != "user":
        # The transcript ends on the agent's own turn: there is no question pending.
        raise ValueError("the last message must be from someone other than the agent")

    return RunInput(
        thread_id=thread_id,
        run_id=run_id,
        user_message=last["content"],
        history=history,
        context_prompt=_context_prompt(body.get("context")),
    )


def _context_prompt(context: Any) -> Optional[str]:
    """Fold the caller's context list into one system-prompt paragraph.

    Truncated hard. It is untrusted text from a conversation, and its job is to tell the
    agent where it is standing, not to become the prompt.
    """
    if not isinstance(context, list):
        return None
    lines: List[str] = []
    for item in context:
        if not isinstance(item, dict):
            continue
        description, value = item.get("description"), item.get("value")
        if isinstance(description, str) and isinstance(value, str) and description and value:
            lines.append(f"{description}: {value}")
    if not lines:
        return None
    joined = "You are answering in a group chat. " + "; ".join(lines) + "."
    return joined[:MAX_CONTEXT_PROMPT_BYTES]


def sanitize_session_key(thread_id: str) -> str:
    """A memory scope derived from the caller's thread, never taken from it verbatim.

    The value reaches file paths and store keys, so it is rebuilt out of safe characters
    rather than validated and passed along.
    """
    return "blob:" + _SESSION_KEY_SAFE.sub("-", thread_id)[:128]


def sse(event: Dict[str, Any]) -> bytes:
    """One SSE frame.

    No ``event:`` line: AG-UI's discriminator is the JSON ``type`` field, and a frame
    that carried both could disagree with itself. A literal newline inside the payload
    would split the frame in two, so the separators are compact and the result is
    checked rather than assumed.
    """
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if "\n" in payload or "\r" in payload:
        payload = payload.replace("\r", "").replace("\n", " ")
    return b"data: " + payload.encode("utf-8") + b"\n\n"


def keepalive() -> bytes:
    """A comment frame. Ignored by every SSE parser; keeps intermediaries from closing."""
    return b": keepalive\n\n"


# --- Event builders. SCREAMING_SNAKE type, camelCase fields, nothing else. -----------
def run_started(thread_id: str, run_id: str) -> Dict[str, Any]:
    return {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}


def run_finished(thread_id: str, run_id: str) -> Dict[str, Any]:
    return {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}


def run_error(message: str, code: Optional[str] = None) -> Dict[str, Any]:
    event: Dict[str, Any] = {"type": "RUN_ERROR", "message": message}
    if code:
        event["code"] = code
    return event


def text_message_start(message_id: str) -> Dict[str, Any]:
    return {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"}


def text_message_content(message_id: str, delta: str) -> Dict[str, Any]:
    return {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": delta}


def text_message_end(message_id: str) -> Dict[str, Any]:
    return {"type": "TEXT_MESSAGE_END", "messageId": message_id}


def tool_call_start(tool_call_id: str, tool_call_name: str) -> Dict[str, Any]:
    return {
        "type": "TOOL_CALL_START",
        "toolCallId": tool_call_id,
        "toolCallName": tool_call_name,
    }


def tool_call_end(tool_call_id: str) -> Dict[str, Any]:
    return {"type": "TOOL_CALL_END", "toolCallId": tool_call_id}


def text_message(message_id: str, text: str) -> List[Dict[str, Any]]:
    """A complete message as the triad the protocol expects."""
    return [
        text_message_start(message_id),
        text_message_content(message_id, text),
        text_message_end(message_id),
    ]


@dataclass
class AGUICallbacks:
    """The callbacks a turn is run with, and what they collected.

    ``accumulated_text`` is not forwarded as it arrives — see the handler for why — but
    it is kept, because it is the only thing that can be salvaged when a turn is cut off
    at the timeout. A truncated answer in the channel beats an error banner.
    """

    stream_delta_callback: Callable[..., None]
    tool_start_callback: Callable[..., None]
    tool_complete_callback: Callable[..., None]
    chunks: List[str] = field(default_factory=list)
    tool_ids: Dict[str, str] = field(default_factory=dict)

    @property
    def accumulated_text(self) -> str:
        return "".join(self.chunks)


def make_agui_callbacks(enqueue: Callable[[Dict[str, Any]], None]) -> AGUICallbacks:
    """Wire the agent's callbacks to an event sink.

    Every one swallows its own exceptions. The agent silences callback errors anyway, so
    raising here would not surface the problem — it would only lose the event and leave
    nothing to find afterwards.
    """
    state: Dict[str, Any] = {}

    def on_delta(delta: Any = "", *_args: Any, **_kwargs: Any) -> None:
        try:
            if isinstance(delta, str) and delta:
                state.setdefault("chunks", []).append(delta)
        except Exception:
            pass

    def on_tool_start(name: Any = "", *_args: Any, **_kwargs: Any) -> None:
        try:
            tool_name = name if isinstance(name, str) and name else "a tool"
            call_id = f"tc_{len(state.setdefault('tools', {})) + 1}"
            state["tools"][tool_name] = call_id
            enqueue(tool_call_start(call_id, tool_name))
        except Exception:
            pass

    def on_tool_complete(name: Any = "", *_args: Any, **_kwargs: Any) -> None:
        try:
            tool_name = name if isinstance(name, str) and name else "a tool"
            call_id = state.get("tools", {}).get(tool_name)
            if call_id:
                enqueue(tool_call_end(call_id))
        except Exception:
            pass

    callbacks = AGUICallbacks(
        stream_delta_callback=on_delta,
        tool_start_callback=on_tool_start,
        tool_complete_callback=on_tool_complete,
    )
    state["chunks"] = callbacks.chunks
    state["tools"] = callbacks.tool_ids
    return callbacks
