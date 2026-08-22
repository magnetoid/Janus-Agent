"""The AG-UI protocol, as arithmetic.

Everything the endpoint decides before a model is involved lives in `gateway.platforms.
agui` as pure functions, so it can be tested without a server, a key, or a turn. What
these cover is mostly the things that fail *silently* if they are wrong: a signature
computed over re-serialised JSON, a wire value in the wrong case, a newline that splits
an SSE frame in half.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from gateway.platforms import agui


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
        events: list = []
        callbacks = agui.make_agui_callbacks(events.append)
        callbacks.tool_start_callback("search_docs")
        callbacks.tool_complete_callback("search_docs")

        assert [e["type"] for e in events] == ["TOOL_CALL_START", "TOOL_CALL_END"]
        assert events[0]["toolCallName"] == "search_docs"
        assert events[0]["toolCallId"] == events[1]["toolCallId"]

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
