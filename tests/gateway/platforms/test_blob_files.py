"""Files a Janus reply hands to Blob.

A reply that names a file — `MEDIA:/path` on its own line, or a bare absolute path — used
to reach Blob as the sentence and nothing else: the Blob connectors sent text only. These
pin the half that was missing. The file travels inside the run as Blob's `blob.file.*`
events (start, base64 chunks, end), chosen by the same rules every other platform uses
(`extract_media`, `extract_local_files`, `validate_media_delivery_path`), plus one of
Blob's own: in a workspace where anybody in a channel can ask, nothing inside Janus's own
state — other conversations' sessions, memories, logs, tokens — is ever attached. And an
explicit file that cannot be sent says so in the reply rather than vanishing.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms import agui, blob_files
from gateway.platforms.blob import BlobAdapter

from .test_agui import _agui_test_app, _post_signed_agui, run_body
from .test_blob import config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Janus home of our own, so the state guard has something real to guard."""
    root = tmp_path / "janus-home"
    root.mkdir()
    monkeypatch.setenv("JANUS_HOME", str(root))
    return root


def made(home: Path, relative: str, data: bytes) -> Path:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


PAGE = b"<!doctype html><html><body><h1>Hadley</h1></body></html>"


class TestTheEvents:
    def test_a_file_is_a_start_its_pieces_and_an_end(self) -> None:
        data = bytes(range(256)) * 1000
        events = agui.file_events("f1", "blob.bin", "application/octet-stream", data, piece=64_000)

        assert events[0] == {
            "type": "CUSTOM",
            "name": "blob.file.start",
            "value": {
                "id": "f1",
                "name": "blob.bin",
                "mimeType": "application/octet-stream",
                "size": len(data),
            },
        }
        assert events[-1] == {"type": "CUSTOM", "name": "blob.file.end", "value": {"id": "f1"}}
        chunks = [e["value"]["data"] for e in events[1:-1]]
        assert all(e["name"] == "blob.file.chunk" and e["value"]["id"] == "f1" for e in events[1:-1])
        assert b"".join(base64.b64decode(c) for c in chunks) == data

    def test_every_piece_fits_a_blob_socket_frame(self) -> None:
        data = b"x" * (3 * agui.FILE_PIECE_BYTES + 5)
        events = agui.file_events("f1", "big.bin", "application/octet-stream", data)

        frames = [json.dumps({"t": "event", "runId": "r", "event": e}) for e in events]
        assert max(len(f.encode()) for f in frames) < 512 * 1024

    def test_an_empty_file_is_still_a_file(self) -> None:
        events = agui.file_events("f1", "empty.txt", "text/plain", b"")
        assert [e["name"] for e in events] == ["blob.file.start", "blob.file.end"]


class TestWhatAReplyHandsOver:
    def test_a_media_tag_becomes_a_file_and_leaves_the_text(self, home: Path) -> None:
        page = made(home, "hadley/index.html", PAGE)

        handed = blob_files.collect(f"Built it.\nMEDIA:{page}")

        assert [(f.name, f.mime, f.data) for f in handed.files] == [("index.html", "text/html", PAGE)]
        assert handed.text == "Built it."
        assert handed.notes == []

    def test_a_bare_path_is_sent_and_stays_in_the_sentence(self, home: Path) -> None:
        # The case that started this: "File: /opt/data/hadley/index.html — a single ...".
        page = made(home, "hadley/index.html", PAGE)
        reply = f"Done. File: {page} — a single self-contained HTML file."

        handed = blob_files.collect(reply)

        assert [f.name for f in handed.files] == ["index.html"]
        assert handed.text == reply

    def test_the_same_file_named_twice_is_sent_once(self, home: Path) -> None:
        page = made(home, "site/index.html", PAGE)

        handed = blob_files.collect(f"See {page}\nMEDIA:{page}")

        assert len(handed.files) == 1

    def test_a_media_tag_for_a_file_that_is_not_there_says_so(self, home: Path) -> None:
        handed = blob_files.collect(f"Here.\nMEDIA:{home}/nowhere/report.pdf")

        assert handed.files == []
        assert any("report.pdf" in note for note in handed.notes)

    def test_a_bare_path_that_is_not_there_is_left_alone(self, home: Path) -> None:
        handed = blob_files.collect(f"I would write it to {home}/later/report.pdf next.")

        assert handed.files == []
        assert handed.notes == []

    @pytest.mark.parametrize(
        "relative",
        [
            "sessions/20260921_abc.json",
            "memories/MEMORY.md",
            "logs/gateway.log.txt",
            "state.db.json",
            "auth.json",
            "config.yaml",
            ".env.txt",
            "profiles/work/config.yaml",
        ],
    )
    def test_nothing_of_janus_own_state_is_ever_attached(self, home: Path, relative: str) -> None:
        state = made(home, relative, b'{"secret": "another conversation"}')

        handed = blob_files.collect(f"Sure.\nMEDIA:{state}")

        assert handed.files == []
        assert any(state.name in note for note in handed.notes)

    def test_a_file_too_large_to_hand_over_says_so(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(blob_files, "MAX_FILE_BYTES", 10)
        big = made(home, "out/big.csv", b"a,b\n" * 10)

        handed = blob_files.collect(f"MEDIA:{big}")

        assert handed.files == []
        assert any("big.csv" in note for note in handed.notes)

    def test_no_more_than_the_cap_of_files(self, home: Path) -> None:
        paths = [made(home, f"out/{n}.csv", b"a\n") for n in range(blob_files.MAX_FILES + 2)]

        handed = blob_files.collect("\n".join(f"MEDIA:{p}" for p in paths))

        assert len(handed.files) == blob_files.MAX_FILES
        assert handed.notes


class TestOverHttp:
    """`_handle_agui` end to end: the files go out ahead of the text they belong to."""

    @pytest.mark.asyncio
    async def test_the_file_is_streamed_before_the_answer(self, home: Path) -> None:
        page = made(home, "hadley/index.html", PAGE)
        adapter, app = _agui_test_app()
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (
                    {"final_response": f"Built it.\nMEDIA:{page}", "messages": [], "api_calls": 1},
                    {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                )
                resp = await _post_signed_agui(cli, run_body())
                body = await resp.text()

        events = [
            json.loads(line[len("data: ") :])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        names = [e.get("name") or e["type"] for e in events]
        assert names.index("blob.file.start") < names.index("TEXT_MESSAGE_START")
        chunks = [e["value"]["data"] for e in events if e.get("name") == "blob.file.chunk"]
        assert b"".join(base64.b64decode(c) for c in chunks) == PAGE
        text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
        assert text == "Built it."

    @pytest.mark.asyncio
    async def test_the_prompt_tells_the_agent_how_to_hand_over_a_file(self) -> None:
        adapter, app = _agui_test_app()
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (
                    {"final_response": "ok", "messages": [], "api_calls": 1},
                    {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                )
                resp = await _post_signed_agui(cli, run_body())
                await resp.text()

        assert "MEDIA:/absolute/path" in mock_run.call_args.kwargs["ephemeral_system_prompt"]


class TestOverTheSocket:
    @pytest.mark.asyncio
    async def test_the_file_goes_down_the_socket_before_the_answer(self, home: Path) -> None:
        page = made(home, "hadley/index.html", PAGE)
        adapter = BlobAdapter(config())
        socket = AsyncMock()

        async def handler(_event):
            return f"Built it.\nMEDIA:{page}"

        adapter.set_message_handler(handler)
        await adapter._handle_run(
            socket,
            {"t": "run", "runId": "run-f", "input": {"threadId": "c1", "messages": [{"role": "user", "content": "build"}]}},
        )

        frames = [json.loads(call.args[0]) for call in socket.send.await_args_list]
        events = [f["event"] for f in frames if f.get("t") == "event"]
        names = [e.get("name") or e["type"] for e in events]
        assert names.index("blob.file.start") < names.index("TEXT_MESSAGE_START")
        chunks = [e["value"]["data"] for e in events if e.get("name") == "blob.file.chunk"]
        assert b"".join(base64.b64decode(c) for c in chunks) == PAGE
        text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
        assert text == "Built it."
        assert max(len(json.dumps(f).encode()) for f in frames) < 512 * 1024
