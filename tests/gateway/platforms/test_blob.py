import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.blob import BlobAdapter, blob_agent_url


def config(**extra):
    return PlatformConfig(enabled=True, token="blob-bot-test", extra={"url": "https://chat.example.com", **extra})


def test_blob_is_a_builtin_platform():
    assert Platform.BLOB.value == "blob"


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://chat.example.com", "wss://chat.example.com/ws/agent"),
        ("http://localhost:8000/", "ws://localhost:8000/ws/agent"),
        ("wss://chat.example.com/ws/agent", "wss://chat.example.com/ws/agent"),
    ],
)
def test_blob_agent_url(base, expected):
    assert blob_agent_url(base) == expected


def test_adapter_requires_url_and_token(monkeypatch):
    monkeypatch.delenv("BLOB_URL", raising=False)
    monkeypatch.delenv("BLOB_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BLOB_URL"):
        BlobAdapter(PlatformConfig(enabled=True, token="token"))
    with pytest.raises(ValueError, match="BLOB_BOT_TOKEN"):
        BlobAdapter(PlatformConfig(enabled=True, extra={"url": "https://blob.test"}))


@pytest.mark.asyncio
async def test_run_becomes_janus_message_and_emits_agui_answer():
    adapter = BlobAdapter(config())
    socket = AsyncMock()
    seen = []

    async def handler(event):
        seen.append(event)
        return "Hello from Janus"

    adapter.set_message_handler(handler)
    frame = {
        "t": "run",
        "runId": "run-1",
        "input": {
            "threadId": "channel-1",
            "runId": "trigger-message-1",
            "messages": [
                {"id": "old", "role": "user", "name": "Ana", "content": "Earlier"},
                {"id": "msg-1", "role": "user", "name": "Marko", "content": "@Janus hello"},
            ],
            "state": None,
            "tools": [],
            "context": [
                {"description": "channel", "value": "general"},
                {"description": "asked_by", "value": "Marko"},
            ],
            "forwardedProps": {},
        },
    }

    await adapter._handle_run(socket, frame)

    assert len(seen) == 1
    event = seen[0]
    assert event.text == "@Janus hello"
    assert event.message_id == "msg-1"
    assert event.source.platform is Platform.BLOB
    assert event.source.chat_id == "channel-1"
    assert event.source.chat_name == "general"
    assert event.source.user_name == "Marko"
    assert event.source.thread_id is None

    frames = [json.loads(call.args[0]) for call in socket.send.await_args_list]
    assert [item["t"] for item in frames] == ["event", "event", "event", "event", "event", "done"]
    assert frames[0]["event"]["type"] == "RUN_STARTED"
    assert frames[2]["event"] == {
        "type": "TEXT_MESSAGE_CONTENT",
        "messageId": frames[1]["event"]["messageId"],
        "delta": "Hello from Janus",
    }
    assert frames[-1] == {"t": "done", "runId": "run-1"}


@pytest.mark.asyncio
async def test_duplicate_run_is_acknowledged_once():
    adapter = BlobAdapter(config())
    socket = AsyncMock()
    handler = AsyncMock(return_value="once")
    adapter.set_message_handler(handler)
    frame = {
        "t": "run",
        "runId": "same-run",
        "input": {
            "threadId": "dm-1",
            "runId": "message-1",
            "messages": [{"id": "message-1", "role": "user", "name": "Marko", "content": "hello"}],
            "context": [{"description": "asked_by", "value": "Marko"}],
        },
    }

    await adapter._handle_run(socket, frame)
    await adapter._handle_run(socket, frame)

    handler.assert_awaited_once()
    done = [json.loads(call.args[0]) for call in socket.send.await_args_list if json.loads(call.args[0])["t"] == "done"]
    assert done == [{"t": "done", "runId": "same-run"}]


@pytest.mark.asyncio
async def test_failure_emits_run_error_then_done():
    adapter = BlobAdapter(config())
    socket = AsyncMock()

    async def handler(_event):
        raise RuntimeError("boom")

    adapter.set_message_handler(handler)
    frame = {
        "t": "run",
        "runId": "run-error",
        "input": {
            "threadId": "channel-1",
            "messages": [{"id": "m", "role": "user", "name": "M", "content": "fail"}],
            "context": [],
        },
    }

    await adapter._handle_run(socket, frame)
    frames = [json.loads(call.args[0]) for call in socket.send.await_args_list]
    assert frames[0]["event"]["type"] == "RUN_STARTED"
    assert frames[-2]["event"]["type"] == "RUN_ERROR"
    assert frames[-1] == {"t": "done", "runId": "run-error"}


@pytest.mark.asyncio
async def test_send_is_not_used_outside_an_active_blob_run():
    adapter = BlobAdapter(config())
    result = await adapter.send("channel-1", "hello")
    assert result == SendResult(success=False, error="Blob replies require an active /ws/agent run")


@pytest.mark.asyncio
async def test_socket_e2e_authenticates_handshakes_runs_and_finishes():
    import websockets

    received = []
    authorized = asyncio.Event()
    finished = asyncio.Event()

    async def server(socket):
        if socket.request.headers.get("Authorization") == "Bearer blob-bot-test":
            authorized.set()
        await socket.send(json.dumps({"t": "ready", "name": "Janus", "scopes": ["chat:write"]}))
        received.append(json.loads(await socket.recv()))
        await socket.send(
            json.dumps(
                {
                    "t": "run",
                    "runId": "e2e-run",
                    "input": {
                        "threadId": "e2e-channel",
                        "runId": "e2e-trigger",
                        "messages": [
                            {"id": "e2e-message", "role": "user", "name": "Marko", "content": "ping"}
                        ],
                        "context": [{"description": "channel", "value": "testing"}],
                    },
                }
            )
        )
        while True:
            frame = json.loads(await socket.recv())
            received.append(frame)
            if frame["t"] == "done":
                finished.set()
                return

    async with websockets.serve(server, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        adapter = BlobAdapter(
            PlatformConfig(
                enabled=True,
                token="blob-bot-test",
                extra={"url": f"http://127.0.0.1:{port}", "reconnect_min_seconds": 0.1},
            )
        )
        adapter.set_message_handler(AsyncMock(return_value="pong"))
        assert await adapter.connect() is True
        await asyncio.wait_for(authorized.wait(), timeout=2)
        await asyncio.wait_for(finished.wait(), timeout=2)
        await adapter.disconnect()

    assert received[0]["t"] == "hello"
    assert received[0]["name"] == "Janus"
    assert received[-1] == {"t": "done", "runId": "e2e-run"}
    event_types = [frame["event"]["type"] for frame in received if frame["t"] == "event"]
    assert event_types == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
