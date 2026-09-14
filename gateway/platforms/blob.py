"""Native Blob messaging adapter using Blob's persistent ``/ws/agent`` protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import uuid
from collections import deque
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

try:
    import websockets
except ImportError:  # pragma: no cover - exercised by requirements check
    websockets = None  # type: ignore[assignment]


def blob_agent_url(base_url: str) -> str:
    """Normalize a Blob HTTP(S)/WS(S) base URL to its agent socket URL."""
    value = (base_url or "").strip()
    if not value:
        raise ValueError("BLOB_URL is required")
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parsed.scheme)
    if scheme is None or not parsed.netloc:
        raise ValueError("BLOB_URL must be an HTTP(S) or WS(S) URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/ws/agent"):
        path = f"{path}/ws/agent" if path else "/ws/agent"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def check_blob_requirements() -> bool:
    return websockets is not None and bool(os.getenv("BLOB_URL")) and bool(os.getenv("BLOB_BOT_TOKEN"))


class BlobAdapter(BasePlatformAdapter):
    """Consume Blob runs and execute them through Janus' normal message handler."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.BLOB)
        extra = config.extra if isinstance(config.extra, dict) else {}
        self.base_url = str(extra.get("url") or os.getenv("BLOB_URL") or "").strip()
        self.token = str(config.token or os.getenv("BLOB_BOT_TOKEN") or "").strip()
        if not self.base_url:
            raise ValueError("BLOB_URL is required")
        if not self.token:
            raise ValueError("BLOB_BOT_TOKEN is required")
        self.socket_url = blob_agent_url(self.base_url)
        self.agent_name = str(extra.get("name") or os.getenv("BLOB_AGENT_NAME") or "Janus")
        self.agent_description = str(
            extra.get("description")
            or os.getenv("BLOB_AGENT_DESCRIPTION")
            or "Janus native AI agent"
        )
        self.agent_version = str(extra.get("version") or os.getenv("BLOB_AGENT_VERSION") or "")
        self._socket: Any = None
        self._serve_task: Optional[asyncio.Task] = None
        self._run_tasks: Dict[str, asyncio.Task] = {}
        self._seen_runs: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._seen_limit = max(100, int(extra.get("dedup_limit", 2000)))
        self._concurrency = asyncio.Semaphore(max(1, int(extra.get("max_concurrent_runs", 4))))
        self._reconnect_min = max(0.1, float(extra.get("reconnect_min_seconds", 1)))
        self._reconnect_max = max(self._reconnect_min, float(extra.get("reconnect_max_seconds", 30)))
        self._heartbeat_seconds = max(5.0, float(extra.get("heartbeat_seconds", 20)))

    @property
    def enforces_own_access_policy(self) -> bool:
        # Blob authenticates the app and applies workspace/channel/owner access policy
        # before publishing a run to this socket.
        return True

    async def connect(self) -> bool:
        if websockets is None:
            logger.error("Blob requires the websockets package")
            return False
        if self._serve_task and not self._serve_task.done():
            return True
        self._running = True
        self._serve_task = asyncio.create_task(self._serve_forever(), name="blob-agent-socket")
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._serve_task:
            self._serve_task.cancel()
        for task in list(self._run_tasks.values()):
            task.cancel()
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        pending = [task for task in [self._serve_task, *self._run_tasks.values()] if task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._serve_task = None
        self._run_tasks.clear()
        self._socket = None

    async def _serve_forever(self) -> None:
        delay = self._reconnect_min
        while self._running:
            try:
                await self._session()
                delay = self._reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Blob socket disconnected: %s", error)
            if not self._running:
                break
            await asyncio.sleep(random.uniform(0, delay))
            delay = min(delay * 2, self._reconnect_max)

    async def _session(self) -> None:
        assert websockets is not None
        async with websockets.connect(
            self.socket_url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=2 * 1024 * 1024,
            open_timeout=30,
            ping_interval=None,
        ) as socket:
            self._socket = socket
            heartbeat = asyncio.create_task(self._heartbeat(socket))
            try:
                async for raw in socket:
                    await self._handle_frame(socket, raw)
            finally:
                self._socket = None
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                for task in list(self._run_tasks.values()):
                    task.cancel()
                if self._run_tasks:
                    await asyncio.gather(*self._run_tasks.values(), return_exceptions=True)
                self._run_tasks.clear()

    async def _heartbeat(self, socket: Any) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await self._send_frame(socket, {"t": "ping"})

    async def _handle_frame(self, socket: Any, raw: Any) -> None:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("t")
        if kind == "ready":
            hello = {
                "t": "hello",
                "name": self.agent_name,
                "description": self.agent_description,
            }
            if self.agent_version:
                hello["version"] = self.agent_version
            await self._send_frame(socket, hello)
        elif kind == "run":
            run_id = frame.get("runId")
            if isinstance(run_id, str) and run_id not in self._seen_runs:
                task = asyncio.create_task(self._handle_run(socket, frame), name=f"blob-run-{run_id}")
                self._run_tasks[run_id] = task
                task.add_done_callback(lambda _task, rid=run_id: self._run_tasks.pop(rid, None))
        elif kind == "cancel":
            task = self._run_tasks.get(str(frame.get("runId") or ""))
            if task:
                task.cancel()
        # pong, hello_ok, error, and future protocol frames are intentionally non-fatal.

    def _remember_run(self, run_id: str) -> bool:
        if run_id in self._seen_runs:
            return False
        self._seen_runs.add(run_id)
        self._seen_order.append(run_id)
        while len(self._seen_order) > self._seen_limit:
            self._seen_runs.discard(self._seen_order.popleft())
        return True

    @staticmethod
    def _context(run_input: Dict[str, Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for item in run_input.get("context") or []:
            if isinstance(item, dict) and item.get("description") and item.get("value") is not None:
                result[str(item["description"])] = str(item["value"])
        return result

    @staticmethod
    def _trigger_message(run_input: Dict[str, Any]) -> Dict[str, Any]:
        messages = run_input.get("messages")
        if not isinstance(messages, list):
            return {}
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user" and message.get("content"):
                return message
        return {}

    def _message_event(self, run_input: Dict[str, Any]) -> MessageEvent:
        message = self._trigger_message(run_input)
        context = self._context(run_input)
        conversation_id = str(run_input.get("threadId") or run_input.get("runId") or "")
        text = str(message.get("content") or "")
        message_id = str(message.get("id") or run_input.get("runId") or "") or None
        user_name = str(message.get("name") or context.get("asked_by") or "") or None
        source = SessionSource(
            platform=Platform.BLOB,
            chat_id=conversation_id,
            chat_name=context.get("channel"),
            chat_type="dm" if context.get("channel", "").lower().startswith("direct") else "channel",
            user_id=user_name,
            user_name=user_name,
            message_id=message_id,
        )
        return MessageEvent(
            text=text,
            message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
            source=source,
            raw_message=run_input,
            message_id=message_id,
        )

    async def _handle_run(self, socket: Any, frame: Dict[str, Any]) -> None:
        run_id = frame.get("runId")
        run_input = frame.get("input")
        if not isinstance(run_id, str) or not isinstance(run_input, dict):
            return
        if not self._remember_run(run_id):
            return
        thread_id = str(run_input.get("threadId") or run_id)
        await self._event(socket, run_id, {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id})
        try:
            if self._message_handler is None:
                raise RuntimeError("Blob adapter has no Janus message handler")
            async with self._concurrency:
                response = await self._message_handler(self._message_event(run_input))
            text, _ttl = self._unwrap_ephemeral(response)
            if text:
                message_id = str(uuid.uuid4())
                await self._event(socket, run_id, {"type": "TEXT_MESSAGE_START", "messageId": message_id})
                await self._event(
                    socket,
                    run_id,
                    {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": text},
                )
                await self._event(socket, run_id, {"type": "TEXT_MESSAGE_END", "messageId": message_id})
            await self._event(socket, run_id, {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._event(socket, run_id, {"type": "RUN_ERROR", "message": "Run cancelled"})
            raise
        except Exception as error:
            logger.exception("Blob run %s failed", run_id)
            await self._event(socket, run_id, {"type": "RUN_ERROR", "message": str(error)[:400]})
        finally:
            with contextlib.suppress(Exception):
                await self._send_frame(socket, {"t": "done", "runId": run_id})

    async def _event(self, socket: Any, run_id: str, event: Dict[str, Any]) -> None:
        await self._send_frame(socket, {"t": "event", "runId": run_id, "event": event})

    @staticmethod
    async def _send_frame(socket: Any, frame: Dict[str, Any]) -> None:
        await socket.send(json.dumps(frame, separators=(",", ":")))

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # Normal replies are returned by _handle_run as AG-UI events. A bare chat_id is
        # insufficient for an unsolicited Blob post because the socket run deliberately
        # does not expose the parent channel when threadId identifies a thread.
        return SendResult(success=False, error="Blob replies require an active /ws/agent run")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"id": chat_id, "type": "blob"}
