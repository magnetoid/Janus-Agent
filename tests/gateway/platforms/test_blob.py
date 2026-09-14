import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.blob import BlobAdapter, blob_agent_url, blob_api_base


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


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://chat.example.com", "https://chat.example.com"),
        ("wss://chat.example.com/ws/agent", "https://chat.example.com"),
        ("http://localhost:8000/", "http://localhost:8000"),
    ],
)
def test_blob_api_base(base, expected):
    """One setting, two transports — the REST origin is derived, never configured twice."""
    assert blob_api_base(base) == expected


def _fake_httpx(monkeypatch, handler):
    """Point the adapter's own AsyncClient at a MockTransport."""
    import httpx as real_httpx

    from gateway.platforms import blob as blob_module

    class _Client(real_httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("limits", None)
            super().__init__(transport=real_httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(blob_module.httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_send_posts_into_blob_over_the_bot_api(monkeypatch):
    """The outbound half: a cron digest has no run to ride on, so it goes over REST."""
    seen = {}

    def handler(request):
        import httpx as real_httpx

        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return real_httpx.Response(201, json={"message": {"id": "m-9"}})

    _fake_httpx(monkeypatch, handler)
    result = await BlobAdapter(config()).send("#general", "Standup is ready", reply_to="thread-1")

    assert result.success is True
    assert result.message_id == "m-9"
    assert seen["url"] == "https://chat.example.com/api/v1/chat.postMessage"
    assert seen["auth"] == "Bearer blob-bot-test"
    assert seen["body"]["channel"] == "#general"
    assert seen["body"]["text"] == "Standup is ready"
    assert seen["body"]["threadRootId"] == "thread-1"
    # Every Blob write is idempotent on this, so base's retry cannot double-post.
    assert seen["body"]["clientMsgId"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retryable"), [(500, True), (429, True), (403, False), (404, False)]
)
async def test_send_only_retries_what_is_worth_retrying(monkeypatch, status, retryable):
    """Retrying a wrong request just spends the rate limit."""

    def handler(request):
        import httpx as real_httpx

        return real_httpx.Response(status, json={"error": {"message": "no"}})

    _fake_httpx(monkeypatch, handler)
    result = await BlobAdapter(config()).send("#general", "hi")
    assert result.success is False
    assert result.retryable is retryable


@pytest.mark.asyncio
async def test_a_working_run_keeps_telling_blob_it_is_alive():
    """The bug this pins: RUN_STARTED then silence until the run ends.

    Blob resets a run's idle deadline only on an event relayed into that run — the
    websocket ping keeps the socket open and touches nothing else — and ends a run that
    has been quiet for AGUI_TIMEOUT_SEC + AGUI_READ_TIMEOUT_SEC, 150s by default. A
    short question answered fast survived that; a run with tools in it did not, and the
    answer was discarded while it was still coming.
    """
    adapter = BlobAdapter(config(run_keepalive_seconds=0.02))
    socket = AsyncMock()

    async def slow(_event):
        await asyncio.sleep(0.15)
        return "done at last"

    adapter.set_message_handler(slow)
    await adapter._handle_run(
        socket,
        {"t": "run", "runId": "run-slow", "input": {"threadId": "c1", "messages": [{"role": "user", "content": "hi"}]}},
    )

    kinds = [
        json.loads(call.args[0]).get("event", {}).get("type")
        for call in socket.send.await_args_list
    ]
    assert kinds[0] == "RUN_STARTED"
    assert "STEP_STARTED" in kinds, "the card would be blank while the first interval elapses"
    assert kinds.count("ACTIVITY_SNAPSHOT") >= 2, kinds
    # And it stops the moment the handler returns, rather than racing the reply.
    assert kinds.index("TEXT_MESSAGE_START") > kinds.index("ACTIVITY_SNAPSHOT")
    assert "ACTIVITY_SNAPSHOT" not in kinds[kinds.index("TEXT_MESSAGE_START") :]
    # The last event is the run ending; the last *frame* after it is `done`, which
    # carries no event at all — that ordering is the protocol and is asserted elsewhere.
    assert [k for k in kinds if k][-1] == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_a_long_reply_is_split_into_frames_blob_will_read():
    """Blob refuses a frame over 512 KiB, and answers with an error this adapter treats
    as non-fatal — so one oversized delta vanished in silence instead of failing."""
    adapter = BlobAdapter(config())
    socket = AsyncMock()
    body = "\u0161" * 20_000  # non-ASCII: json escapes each to six bytes

    async def handler(_event):
        return body

    adapter.set_message_handler(handler)
    await adapter._handle_run(
        socket,
        {"t": "run", "runId": "run-long", "input": {"threadId": "c1", "messages": [{"role": "user", "content": "hi"}]}},
    )

    frames = [json.loads(call.args[0]) for call in socket.send.await_args_list]
    deltas = [f["event"]["delta"] for f in frames if f.get("event", {}).get("type") == "TEXT_MESSAGE_CONTENT"]
    assert len(deltas) == 3
    assert "".join(deltas) == body
    # One message id for the whole reply, or Blob would post three separate messages.
    ids = {f["event"]["messageId"] for f in frames if "messageId" in f.get("event", {})}
    assert len(ids) == 1
    assert max(len(json.dumps(f).encode()) for f in frames) < 512 * 1024


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
