"""A2A server on the API server (plan item 3.7)."""
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _adapter(a2a=True, key="sk-secret"):
    extra = {"key": key} if key else {}
    if a2a:
        extra["a2a"] = True
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _app(adapter):
    app = web.Application()
    adapter._register_a2a_routes(app)
    return app


def _rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def _send_params(text="What is 2+2?", context_id=None):
    msg = {"role": "user", "messageId": "m-1", "parts": [{"kind": "text", "text": text}]}
    if context_id:
        msg["contextId"] = context_id
    return {"message": msg}


AUTH = {"Authorization": "Bearer sk-secret"}


def test_routes_exist_only_when_enabled():
    disabled = web.Application()
    _adapter(a2a=False)._register_a2a_routes(disabled)
    assert not [r for r in disabled.router.routes()]


@pytest.mark.asyncio
async def test_agent_card_is_public_and_describes_the_endpoint():
    async with TestClient(TestServer(_app(_adapter()))) as cli:
        resp = await cli.get("/.well-known/agent-card.json")
        assert resp.status == 200
        card = await resp.json()
        legacy = await (await cli.get("/.well-known/agent.json")).json()
    assert card == legacy
    assert card["name"] and card["url"].endswith("/a2a")
    assert card["capabilities"]["streaming"] is False
    assert "text/plain" in card["defaultInputModes"]
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert card["skills"] and all("id" in s and "name" in s for s in card["skills"])


@pytest.mark.asyncio
async def test_rpc_requires_the_bearer_key():
    adapter = _adapter()
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post("/a2a", json=_rpc("message/send", _send_params()))
        assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["message/send", "SendMessage"])
async def test_message_send_runs_the_agent_and_returns_a_completed_task(method):
    adapter = _adapter()
    adapter._run_agent = AsyncMock(return_value=({"final_response": "4"}, {"total_tokens": 3}))
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post("/a2a", json=_rpc(method, _send_params()), headers=AUTH)
        body = await resp.json()
    task = body["result"]
    assert body["id"] == 1 and task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0] == {"kind": "text", "text": "4"}
    assert task["contextId"]
    kwargs = adapter._run_agent.await_args.kwargs
    assert kwargs["user_message"] == "What is 2+2?"
    assert kwargs["session_id"] == task["contextId"]


@pytest.mark.asyncio
async def test_context_id_continues_the_same_session():
    adapter = _adapter()
    adapter._run_agent = AsyncMock(return_value=({"final_response": "ok"}, {}))
    adapter._conversation_history_for_session = lambda sid: [{"role": "user", "content": "earlier"}]
    async with TestClient(TestServer(_app(adapter))) as cli:
        await cli.post("/a2a", json=_rpc("message/send", _send_params(context_id="ctx-7")), headers=AUTH)
    kwargs = adapter._run_agent.await_args.kwargs
    assert kwargs["session_id"] == "ctx-7"
    assert kwargs["conversation_history"] == [{"role": "user", "content": "earlier"}]


@pytest.mark.asyncio
async def test_tasks_get_returns_the_stored_task_and_cancel_is_refused():
    adapter = _adapter()
    adapter._run_agent = AsyncMock(return_value=({"final_response": "done"}, {}))
    async with TestClient(TestServer(_app(adapter))) as cli:
        sent = await (await cli.post("/a2a", json=_rpc("message/send", _send_params()), headers=AUTH)).json()
        tid = sent["result"]["id"]
        got = await (await cli.post("/a2a", json=_rpc("tasks/get", {"id": tid}), headers=AUTH)).json()
        missing = await (await cli.post("/a2a", json=_rpc("GetTask", {"id": "nope"}), headers=AUTH)).json()
        cancel = await (await cli.post("/a2a", json=_rpc("tasks/cancel", {"id": tid}), headers=AUTH)).json()
    assert got["result"]["id"] == tid and got["result"]["status"]["state"] == "completed"
    assert missing["error"]["code"] == -32001
    assert cancel["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_agent_failure_is_a_failed_task_not_a_500():
    adapter = _adapter()
    adapter._run_agent = AsyncMock(side_effect=RuntimeError("provider down"))
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post("/a2a", json=_rpc("message/send", _send_params()), headers=AUTH)
        body = await resp.json()
    assert resp.status == 200
    assert body["result"]["status"]["state"] == "failed"


@pytest.mark.asyncio
async def test_protocol_errors_use_json_rpc_codes():
    adapter = _adapter()
    async with TestClient(TestServer(_app(adapter))) as cli:
        bad_json = await (await cli.post("/a2a", data="{not json", headers={**AUTH, "Content-Type": "application/json"})).json()
        unknown = await (await cli.post("/a2a", json=_rpc("does/not/exist"), headers=AUTH)).json()
        no_text = await (await cli.post("/a2a", json=_rpc("message/send", {"message": {"role": "user", "parts": []}}), headers=AUTH)).json()
    assert bad_json["error"]["code"] == -32700
    assert unknown["error"]["code"] == -32601
    assert no_text["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_capabilities_report_whether_a2a_is_on():
    for enabled in (True, False):
        adapter = _adapter(a2a=enabled)
        app = web.Application()
        app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
        async with TestClient(TestServer(app)) as cli:
            caps = await (await cli.get("/v1/capabilities", headers=AUTH)).json()
        assert caps["features"]["a2a"] is enabled
