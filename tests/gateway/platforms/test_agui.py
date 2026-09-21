"""The AG-UI protocol, as arithmetic.

Everything the endpoint decides before a model is involved lives in `gateway.platforms.
agui` as pure functions, so it can be tested without a server, a key, or a turn. What
these cover is mostly the things that fail *silently* if they are wrong: a signature
computed over re-serialised JSON, a wire value in the wrong case, a newline that splits
an SSE frame in half.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import agui
from gateway.platforms import api_server


SECRET = "s" * 64


def sign(body: bytes, timestamp: int, secret: str = SECRET) -> str:
    base = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


class TestSignature:
    def test_a_correct_signature_verifies(self) -> None:
        body, now = b'{"threadId":"t"}', int(time.time())
        assert agui.verify_blob_signature(
            raw_body=body, timestamp=str(now), signature=sign(body, now), secret=SECRET
        )

    def test_the_digest_covers_the_exact_bytes(self) -> None:
        # The reason the handler reads the raw body before parsing: re-serialising a
        # parsed body changes whitespace and key order, and the digest with it.
        original = b'{"threadId": "t", "runId": "r"}'
        now = int(time.time())
        signature = sign(original, now)
        reserialised = json.dumps(json.loads(original), separators=(",", ":")).encode()

        assert reserialised != original
        with pytest.raises(agui.AGUISignatureError):
            agui.verify_blob_signature(
                raw_body=reserialised, timestamp=str(now), signature=signature, secret=SECRET
            )

    def test_an_old_timestamp_is_refused(self) -> None:
        # What stops a captured request being replayed with a fresh header.
        body, old = b"{}", int(time.time()) - 4000
        with pytest.raises(agui.AGUISignatureError) as caught:
            agui.verify_blob_signature(
                raw_body=body, timestamp=str(old), signature=sign(body, old), secret=SECRET
            )
        assert caught.value.reason == "stale_timestamp"

    def test_a_wrong_secret_is_refused(self) -> None:
        body, now = b"{}", int(time.time())
        with pytest.raises(agui.AGUISignatureError):
            agui.verify_blob_signature(
                raw_body=body,
                timestamp=str(now),
                signature=sign(body, now, "other"),
                secret=SECRET,
            )

    def test_without_a_configured_secret_nothing_verifies(self) -> None:
        # Belt and braces: the route is not registered at all in this state.
        with pytest.raises(agui.AGUISignatureError) as caught:
            agui.verify_blob_signature(
                raw_body=b"{}", timestamp="1", signature="v0=x", secret=""
            )
        assert caught.value.reason == "no_secret_configured"


def run_body(**overrides: object) -> dict:
    body = {
        "threadId": "c1",
        "runId": "m1",
        "state": None,
        "messages": [
            {"id": "1", "role": "user", "content": "morning", "name": "Ana"},
            {"id": "2", "role": "assistant", "content": "morning to you"},
            {"id": "3", "role": "user", "content": "when is standup?", "name": "Ana"},
        ],
        "tools": [],
        "context": [{"description": "channel", "value": "general"}],
        "forwardedProps": {},
    }
    body.update(overrides)
    return body


def _agui_test_app() -> tuple[api_server.APIServerAdapter, web.Application]:
    """A real adapter and app with only `/v1/agui` registered — everything
    `_handle_agui` needs, and nothing `start()` would also bring up."""
    adapter = api_server.APIServerAdapter(
        PlatformConfig(enabled=True, extra={"blob_signing_secret": SECRET})
    )
    mws = [
        mw
        for mw in (api_server.cors_middleware, api_server.security_headers_middleware)
        if mw is not None
    ]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/agui", adapter._handle_agui)
    return adapter, app


async def _post_signed_agui(cli: TestClient, body: dict):
    """Sign `body` the way Blob does and POST it — the only way `/v1/agui` accepts one."""
    raw = json.dumps(body).encode()
    timestamp = str(int(time.time()))
    return await cli.post(
        "/v1/agui",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Blob-Request-Timestamp": timestamp,
            "X-Blob-Signature": sign(raw, int(timestamp)),
        },
    )


class TestParsing:
    def test_the_last_message_is_the_question_and_the_rest_is_history(self) -> None:
        run = agui.parse_run_input(run_body())
        assert run.user_message == "Ana: when is standup?"
        assert [m["role"] for m in run.history] == ["user", "assistant"]

    def test_the_speaker_is_named_because_the_message_shape_has_nowhere_else(self) -> None:
        # In a group conversation "who said this" is not decoration.
        run = agui.parse_run_input(run_body())
        assert run.history[0]["content"].startswith("Ana: ")

    def test_the_agents_own_turns_stay_assistant(self) -> None:
        run = agui.parse_run_input(run_body())
        assert run.history[1] == {"role": "assistant", "content": "morning to you"}

    def test_context_becomes_one_bounded_line(self) -> None:
        run = agui.parse_run_input(run_body())
        assert run.context_prompt is not None
        assert "channel: general" in run.context_prompt
        assert len(run.context_prompt) <= agui.MAX_CONTEXT_PROMPT_BYTES

    def test_a_transcript_ending_on_the_agent_is_refused(self) -> None:
        # There is no question pending, so running a turn would be inventing one.
        body = run_body(messages=[{"id": "1", "role": "assistant", "content": "hi"}])
        with pytest.raises(ValueError):
            agui.parse_run_input(body)

    def test_empty_and_malformed_messages_are_skipped_not_fatal(self) -> None:
        body = run_body(
            messages=[
                "not an object",
                {"id": "1", "role": "user", "content": "   "},
                {"id": "2", "role": "user", "content": "real question"},
            ]
        )
        assert agui.parse_run_input(body).user_message == "real question"

    def test_missing_ids_are_refused(self) -> None:
        for missing in ("threadId", "runId"):
            with pytest.raises(ValueError):
                agui.parse_run_input(run_body(**{missing: ""}))


class TestInstructions:
    """`RunInput.instructions`: forwardedProps.instructions, stripped and capped."""

    def test_instructions_are_read_from_forwarded_props(self) -> None:
        run = agui.parse_run_input(run_body(forwardedProps={"instructions": "  Be brief.  "}))
        assert run.instructions == "Be brief."

    def test_instructions_that_are_not_a_string_are_ignored(self) -> None:
        run = agui.parse_run_input(run_body(forwardedProps={"instructions": ["no"]}))
        assert run.instructions is None

    def test_instructions_are_capped(self) -> None:
        run = agui.parse_run_input(run_body(forwardedProps={"instructions": "x" * 5000}))
        assert len(run.instructions) == agui.MAX_INSTRUCTIONS_CHARS


class TestInstructionsEdgeCases:
    """Every shape that must not become an instruction, pinned in one place: a run with
    none of these behaves exactly as a run predating this feature."""

    @pytest.mark.parametrize(
        "forwarded_props",
        [{"instructions": ""}, {"instructions": "   "}, "not a dict"],
        ids=["empty-string", "whitespace-only", "forwardedProps-not-a-dict"],
    )
    def test_instructions_is_none(self, forwarded_props: object) -> None:
        run = agui.parse_run_input(run_body(forwardedProps=forwarded_props))
        assert run.instructions is None

    def test_instructions_is_none_when_forwarded_props_is_absent(self) -> None:
        # Not the {} the rest of this module's bodies send deliberately — the key
        # missing entirely, as every caller before this feature existed always sent it.
        body = run_body()
        del body["forwardedProps"]
        assert agui.parse_run_input(body).instructions is None


class TestEphemeralPrompt:
    """The per-run system prompt: the workspace's instructions, then the room's context,
    then how to hand somebody a file — which every Blob run needs, so it is always there."""

    def test_instructions_come_before_the_context(self) -> None:
        run = agui.parse_run_input(run_body(forwardedProps={"instructions": "Be brief."}))
        prompt = agui.ephemeral_prompt(run)
        assert prompt is not None
        assert prompt.startswith(agui.INSTRUCTIONS_HEADING)
        assert prompt.index("Be brief.") < prompt.index("channel: general")

    def test_the_context_alone_when_there_are_no_instructions(self) -> None:
        run = agui.parse_run_input(run_body())
        assert run.instructions is None
        assert agui.ephemeral_prompt(run) == f"{run.context_prompt}\n\n{agui.FILE_DELIVERY_HINT}"

    def test_with_neither_the_prompt_is_only_how_to_hand_over_a_file(self) -> None:
        run = agui.parse_run_input(run_body(context=[]))
        assert run.context_prompt is None
        assert agui.ephemeral_prompt(run) == agui.FILE_DELIVERY_HINT


class TestAGUIHandler:
    """`_handle_agui` end to end: a real signed POST, `_run_agent` mocked, asserting on
    the `ephemeral_system_prompt` kwarg it receives — the one line this feature adds to
    the handler. Everything else in this module tests `agui.py`'s pure functions without
    a server, by design (see the module docstring); this class is the deliberate
    exception, because the wiring it checks lives in `api_server.py`, not here, and
    reading it is not the same as watching a signed request reach it.
    """

    @staticmethod
    def _mock_result() -> tuple:
        return (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )

    @pytest.mark.asyncio
    async def test_instructions_reach_run_agent_ahead_of_the_context(self) -> None:
        adapter, app = _agui_test_app()
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = self._mock_result()
                resp = await _post_signed_agui(
                    cli, run_body(forwardedProps={"instructions": "Be brief."})
                )
                assert resp.status == 200
                await resp.text()  # drain the SSE body so the handler has returned

        prompt = mock_run.call_args.kwargs["ephemeral_system_prompt"]
        assert prompt.startswith(agui.INSTRUCTIONS_HEADING)
        assert "Be brief." in prompt
        assert prompt.endswith(
            "You are answering in a group chat. channel: general.\n\n" + agui.FILE_DELIVERY_HINT
        )

    @pytest.mark.asyncio
    async def test_without_instructions_the_context_reaches_run_agent_alone(self) -> None:
        adapter, app = _agui_test_app()
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = self._mock_result()
                resp = await _post_signed_agui(cli, run_body())
                assert resp.status == 200
                await resp.text()

        assert mock_run.call_args.kwargs["ephemeral_system_prompt"] == (
            "You are answering in a group chat. channel: general.\n\n" + agui.FILE_DELIVERY_HINT
        )


class TestFraming:
    def test_a_frame_is_one_record(self) -> None:
        frame = agui.sse({"type": "RUN_STARTED", "threadId": "t", "runId": "r"})
        assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
        assert frame.count(b"\n\n") == 1

    def test_a_newline_in_the_payload_cannot_split_the_frame(self) -> None:
        # A literal newline inside a data: line ends the record early, and the parser on
        # the other side would see two events, one of them truncated JSON.
        frame = agui.sse(agui.text_message_content("m", "line one\nline two"))
        assert frame.count(b"\n\n") == 1
        assert json.loads(frame[len(b"data: ") : -2].decode())["delta"]

    def test_unicode_survives(self) -> None:
        frame = agui.sse(agui.text_message_content("m", "Miloš Đorđević"))
        assert json.loads(frame[len(b"data: ") : -2].decode())["delta"] == "Miloš Đorđević"

    def test_the_wire_uses_screaming_snake_and_camel_case(self) -> None:
        # The docs head each section with the TypeScript interface name; matching those
        # instead produces a stream the client parses as nothing at all.
        assert agui.run_started("t", "r")["type"] == "RUN_STARTED"
        assert agui.text_message_start("m")["type"] == "TEXT_MESSAGE_START"
        assert "messageId" in agui.text_message_start("m")
        assert "toolCallName" in agui.tool_call_start("tc1", "search")
        assert agui.text_message_start("m")["role"] == "assistant"

    def test_a_message_is_the_triad_the_protocol_expects(self) -> None:
        assert [e["type"] for e in agui.text_message("m", "hello")] == [
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
        ]

    def test_keepalive_is_a_comment_not_an_event(self) -> None:
        assert agui.keepalive().startswith(b":")


class TestSessionKey:
    def test_the_scope_is_rebuilt_rather_than_trusted(self) -> None:
        # It reaches file paths and store keys, so it is constructed from safe characters
        # instead of being validated and passed along.
        assert agui.sanitize_session_key("../../etc/passwd") == "blob:.._.._etc_passwd".replace(
            "_", "-"
        )
        assert agui.sanitize_session_key("c1").startswith("blob:")

    def test_it_is_bounded(self) -> None:
        assert len(agui.sanitize_session_key("x" * 500)) <= 133


class TestCallbacks:
    def test_deltas_accumulate_for_the_salvage_path(self) -> None:
        events: list = []
        callbacks = agui.make_agui_callbacks(events.append)
        callbacks.stream_delta_callback("par")
        callbacks.stream_delta_callback("tial")

        assert callbacks.accumulated_text == "partial"
        # Deltas are not forwarded: the client buffers anyway, and the model layer drops
        # text on steps that also produced a tool call.
        assert events == []

    def test_a_tool_call_becomes_a_start_and_an_end(self) -> None:
        # The agent's real invocation: `(tool_call_id, name, args)` on start and the same
        # plus the result on completion (`agent/tool_executor.py`). This test used to pass
        # one positional and call it the name — which is what the adapter did too, so the
        # two agreed with each other and disagreed with the agent, and the run card showed
        # call ids where tool names belonged.
        events: list = []
        callbacks = agui.make_agui_callbacks(events.append)
        callbacks.tool_start_callback("call_7", "search_docs", {"q": "socket"})
        callbacks.tool_complete_callback("call_7", "search_docs", {"q": "socket"}, "3 hits")

        assert [e["type"] for e in events] == [
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "TOOL_CALL_RESULT",
            "TOOL_CALL_END",
        ]
        assert events[0]["toolCallName"] == "search_docs"
        assert {e["toolCallId"] for e in events} == {"call_7"}

    def test_a_callback_that_is_handed_nonsense_does_not_raise(self) -> None:
        # The agent silences callback exceptions, so raising here would lose the event
        # and leave nothing to find afterwards.
        callbacks = agui.make_agui_callbacks(lambda _event: None)
        callbacks.stream_delta_callback(None)
        callbacks.tool_start_callback(None)
        callbacks.tool_complete_callback(None)


class TestDurationParsing:
    """These are read while the adapter is constructed, so a bad value used to be an
    outage for every gateway platform rather than a default for this one."""

    def test_a_good_value_is_used(self) -> None:
        assert agui.positive_float("45", 100.0) == 45.0

    def test_a_typo_falls_back_instead_of_raising(self) -> None:
        for bad in ("15s", "", None, "abc", "-1", "0"):
            assert agui.positive_float(bad, 100.0) == 100.0


class TestReadingTheBody:
    """The read that feeds the signature check.

    `read_capped_body` lives in `api_server` rather than here because it takes a stream,
    but what it protects is this module's contract: `verify_blob_signature` hashes the
    raw bytes, so a body read short verifies a different message than the one that was
    signed. From outside, that is indistinguishable from a wrong secret — which is what
    made the shipped bug expensive. It rejected every request that did not arrive in a
    single chunk, and the secret is the first and last thing anyone checks.

    The stream is a stand-in rather than a real `StreamReader`: `read_capped_body` calls
    exactly one method on it, and faking that keeps these tests runnable without aiohttp
    installed, the way the rest of this file already runs without a server.
    """

    class _Stream:
        """Hands back one queued chunk per `readany()`, then EOF — an aiohttp stream's
        contract, which is the whole surface under test."""

        def __init__(self, *chunks: bytes) -> None:
            self._chunks = list(chunks)

        async def readany(self) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

    @staticmethod
    def _split(body: bytes, size: int) -> "list[bytes]":
        return [body[i : i + size] for i in range(0, len(body), size)]

    def test_a_body_split_across_chunks_is_read_whole(self) -> None:
        # The regression. One chunk always worked; more than one did not.
        body = json.dumps({"threadId": "t", "runId": "r", "pad": "x" * 40_000}).encode()
        pieces = self._split(body, 4096)
        assert len(pieces) > 1

        read = asyncio.run(
            api_server.read_capped_body(self._Stream(*pieces), len(body) + 1)
        )

        assert read == body

    def test_the_signature_still_verifies_over_a_chunked_body(self) -> None:
        # The point of the fix: same bytes in, same digest out.
        body = json.dumps({"threadId": "t", "pad": "y" * 20_000}).encode()
        timestamp = int(time.time())
        signature = sign(body, timestamp)

        read = asyncio.run(
            api_server.read_capped_body(
                self._Stream(*self._split(body, 1024)), agui.AGUI_MAX_BODY_BYTES + 1
            )
        )

        assert agui.verify_blob_signature(
            raw_body=read, timestamp=str(timestamp), signature=signature, secret=SECRET
        )

    def test_an_empty_body_reads_as_empty_rather_than_hanging(self) -> None:
        assert asyncio.run(api_server.read_capped_body(self._Stream(), 1024)) == b""

    def test_an_oversized_body_still_exceeds_the_limit_so_the_caller_can_refuse(
        self,
    ) -> None:
        # The ceiling `read(n)` used to provide has to survive the fix, or the 413 path
        # silently stops firing.
        body = b"z" * 5_000

        read = asyncio.run(
            api_server.read_capped_body(self._Stream(*self._split(body, 500)), 1_001)
        )

        assert len(read) > 1_000


class TestToolCallbacks:
    """What a tool call looks like on Blob's run card.

    The agent invokes these as ``(tool_call_id, name, args)`` and
    ``(tool_call_id, name, args, result)`` — ``agent/tool_executor.py``. The first
    version of the adapter read one positional and called it the name, so the card showed
    call ids where names belonged; and it minted ids keyed by name, so two calls to one
    tool shared an id and the second end closed the first. These pin the real contract.
    """

    def _run(self, *calls):
        from gateway.platforms.agui import make_agui_callbacks

        events = []
        cb = make_agui_callbacks(events.append)
        for kind, args in calls:
            (cb.tool_start_callback if kind == "start" else cb.tool_complete_callback)(*args)
        return events

    def test_the_name_is_the_name_and_the_id_is_the_id(self) -> None:
        events = self._run(("start", ("call_1", "web_search", {"q": "blob"})))
        start = events[0]
        assert start["type"] == "TOOL_CALL_START"
        assert start["toolCallId"] == "call_1"
        assert start["toolCallName"] == "web_search"

    def test_two_calls_to_one_tool_keep_their_own_ids(self) -> None:
        events = self._run(
            ("start", ("call_1", "web_search", {"q": "a"})),
            ("start", ("call_2", "web_search", {"q": "b"})),
            ("complete", ("call_1", "web_search", {"q": "a"}, "first")),
            ("complete", ("call_2", "web_search", {"q": "b"}, "second")),
        )
        ends = [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_END"]
        assert ends == ["call_1", "call_2"]

    def test_args_and_result_reach_the_card(self) -> None:
        events = self._run(
            ("start", ("call_1", "web_search", {"q": "blob"})),
            ("complete", ("call_1", "web_search", {"q": "blob"}, {"hits": 3})),
        )
        kinds = [e["type"] for e in events]
        assert kinds == ["TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_RESULT", "TOOL_CALL_END"]
        # The fields Blob's CardFold reads: `delta` on ARGS, `content` on RESULT.
        assert '"q": "blob"' in events[1]["delta"]
        assert '"hits": 3' in events[2]["content"]
        assert events[2]["role"] == "tool"

    def test_a_huge_result_is_capped_not_fatal(self) -> None:
        from gateway.platforms.agui import TOOL_RESULT_CHARS

        events = self._run(
            ("start", ("call_1", "read_file", {})),
            ("complete", ("call_1", "read_file", {}, "x" * (TOOL_RESULT_CHARS * 3))),
        )
        result = next(e for e in events if e["type"] == "TOOL_CALL_RESULT")
        assert len(result["content"]) == TOOL_RESULT_CHARS

    def test_a_complete_for_an_unknown_id_is_ignored(self) -> None:
        events = self._run(("complete", ("never_started", "web_search", {}, "r")))
        assert events == []
