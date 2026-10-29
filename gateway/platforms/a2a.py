"""A2A (Agent2Agent) protocol helpers for the API server (plan item 3.7).

The API server exposes Janus as an A2A agent when ``a2a`` is enabled:

* ``GET /.well-known/agent-card.json`` (and the older ``agent.json`` path)
  — the public agent card: what Janus is, where to call it, how to
  authenticate (the API server's bearer key).
* ``POST /a2a`` — JSON-RPC 2.0, bearer-authenticated. ``message/send``
  runs one agent turn and returns a completed (or failed) ``Task``;
  ``tasks/get`` returns a recent task; ``tasks/cancel`` is refused because
  tasks finish within the request. The v1-style method names
  (``SendMessage``, ``GetTask``, ``CancelTask``) are accepted as aliases.

A2A ``contextId`` maps to a Janus session id, so follow-up messages in the
same context continue the conversation. Streaming and push notifications
are not offered (the card says so).
"""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = "0.3.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002

METHODS = {
    "message/send": "send", "SendMessage": "send",
    "tasks/get": "get", "GetTask": "get",
    "tasks/cancel": "cancel", "CancelTask": "cancel",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def agent_card(base_url: str, *, name: str, version: str, description: str) -> Dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": base_url.rstrip("/") + "/a2a",
        "preferredTransport": "JSONRPC",
        "version": version,
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "security": [{"bearer": []}],
        "skills": [{
            "id": "janus-agent",
            "name": "General-purpose agent",
            "description": ("Answers questions and carries out multi-step tasks with Janus' "
                            "tools (terminal, files, web, skills, memory) on the host it runs on."),
            "tags": ["assistant", "tools", "automation"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        }],
    }


def rpc_result(rid: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def message_text(message: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(text, context_id)`` from an A2A user message, or ``(None, error)``."""
    if not isinstance(message, dict):
        return None, "params.message must be an object"
    texts: List[str] = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and (part.get("kind") or part.get("type")) == "text":
            if isinstance(part.get("text"), str) and part["text"].strip():
                texts.append(part["text"])
    if not texts:
        return None, "params.message needs at least one non-empty text part"
    return "\n\n".join(texts), None


def build_task(*, context_id: str, user_message: Dict[str, Any], state: str,
               text: str) -> Dict[str, Any]:
    task_id = uuid.uuid4().hex
    agent_msg = {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
                 "parts": [{"kind": "text", "text": text}], "contextId": context_id,
                 "taskId": task_id}
    task: Dict[str, Any] = {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": state, "timestamp": now_iso()},
        "history": [dict(user_message, contextId=context_id, taskId=task_id), agent_msg],
    }
    if state == "completed":
        task["artifacts"] = [{"artifactId": uuid.uuid4().hex, "name": "response",
                              "parts": [{"kind": "text", "text": text}]}]
    else:
        task["status"]["message"] = agent_msg
    return task


class TaskStore:
    """Small in-memory LRU of recent tasks for ``tasks/get``."""

    def __init__(self, max_size: int = 256):
        self._max = max_size
        self._tasks: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def put(self, task: Dict[str, Any]) -> None:
        self._tasks[task["id"]] = task
        self._tasks.move_to_end(task["id"])
        while len(self._tasks) > self._max:
            self._tasks.popitem(last=False)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(task_id)
