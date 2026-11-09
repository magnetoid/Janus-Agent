"""/branch can open the branch in a sibling thread instead of replacing the chat's session."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key


def _source(platform=Platform.DISCORD, chat_type="group", chat_id="c1", thread_id=None):
    return SessionSource(
        platform=platform,
        user_id="u1",
        chat_id=chat_id,
        user_name="tester",
        chat_type=chat_type,
        thread_id=thread_id,
    )


def _entry(session_id, source):
    return SessionEntry(
        session_key=build_session_key(source),
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
    )


def _runner(*, config=None, thread_source=None, open_raises=False):
    from gateway.run import GatewayRunner

    source = _source()
    runner = object.__new__(GatewayRunner)
    adapter = MagicMock()
    if open_raises:
        adapter.open_branch_thread = AsyncMock(side_effect=RuntimeError("no perms"))
    else:
        adapter.open_branch_thread = AsyncMock(return_value=thread_source)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = config if config is not None else {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _entry("current-session", source)
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    runner.session_store.switch_session.return_value = _entry("branched", source)
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = "Current Work"
    runner._session_db.get_next_title_in_lineage.return_value = "Current Work #2"
    return runner, adapter


def _event(text):
    return MessageEvent(text=text, source=_source(), message_id="m1")


THREAD = _source(chat_type="thread", chat_id="t9", thread_id="t9")


# ── Runner ───────────────────────────────────────────────────────────────


class TestBranchCommand:
    @pytest.mark.asyncio
    async def test_thread_flag_binds_the_branch_to_a_new_thread(self):
        runner, adapter = _runner(thread_source=THREAD)

        result = await runner._handle_branch_command(_event("/branch --thread Try Redis"))

        adapter.open_branch_thread.assert_awaited_once()
        args = adapter.open_branch_thread.await_args
        assert args.args[1] == "Try Redis"
        assert "Try Redis" in args.args[2]  # intro posted into the thread
        new_session_id = runner._session_db.create_session.call_args.kwargs["session_id"]
        runner.session_store.bind_session.assert_called_once_with(THREAD, new_session_id)
        # The original chat keeps its session.
        runner.session_store.switch_session.assert_not_called()
        runner._session_db.set_session_title.assert_called_once_with(new_session_id, "Try Redis")
        assert "thread" in result.lower()

    @pytest.mark.asyncio
    async def test_default_is_in_place(self):
        runner, adapter = _runner(thread_source=THREAD)

        result = await runner._handle_branch_command(_event("/branch"))

        adapter.open_branch_thread.assert_not_called()
        runner.session_store.switch_session.assert_called_once()
        assert "Branched to" in result

    @pytest.mark.asyncio
    async def test_config_makes_threads_the_default_and_here_opts_out(self):
        cfg = GatewayConfig(branch_in_thread=True)
        runner, adapter = _runner(config=cfg, thread_source=THREAD)
        await runner._handle_branch_command(_event("/branch"))
        adapter.open_branch_thread.assert_awaited_once()

        runner, adapter = _runner(config=cfg, thread_source=THREAD)
        await runner._handle_branch_command(_event("/branch --here named"))
        adapter.open_branch_thread.assert_not_called()
        runner.session_store.switch_session.assert_called_once()
        assert runner._session_db.set_session_title.call_args.args[1] == "named"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raises", [False, True])
    async def test_no_thread_available_falls_back_in_place(self, raises):
        runner, adapter = _runner(thread_source=None, open_raises=raises)

        result = await runner._handle_branch_command(_event("/branch --thread"))

        runner.session_store.bind_session.assert_not_called()
        runner.session_store.switch_session.assert_called_once()
        assert "Branched to" in result
        assert "thread" in result.lower()


# ── SessionStore.bind_session ────────────────────────────────────────────


class TestBindSession:
    def _store(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        store._db = MagicMock()
        return store

    def test_new_key_points_at_the_existing_session(self, tmp_path):
        store = self._store(tmp_path)

        entry = store.bind_session(THREAD, "branched-1")

        assert entry.session_id == "branched-1"
        assert store.get_or_create_session(THREAD).session_id == "branched-1"
        store._db.create_session.assert_not_called()

    def test_rebinding_ends_the_previous_session(self, tmp_path):
        store = self._store(tmp_path)
        old = store.get_or_create_session(THREAD).session_id

        store.bind_session(THREAD, "branched-2")

        store._db.end_session.assert_called_once_with(old, "session_switch")
        assert store.get_or_create_session(THREAD).session_id == "branched-2"


# ── Adapters ─────────────────────────────────────────────────────────────


class TestBaseAdapter:
    @pytest.mark.asyncio
    async def test_platforms_without_threads_return_none(self):
        from gateway.platforms.base import BasePlatformAdapter

        class _NoThreads(BasePlatformAdapter):
            connect = disconnect = get_chat_info = send = None

        adapter = object.__new__(_NoThreads)
        assert await adapter.open_branch_thread(_source(), "t", "intro") is None


class TestSlackAndMatrix:
    def _slack(self, post_result):
        from gateway.platforms.slack import SlackAdapter

        adapter = object.__new__(SlackAdapter)
        adapter.platform = Platform.SLACK
        adapter.config = PlatformConfig(enabled=True, token="***")
        adapter._app = SimpleNamespace(client=None)
        adapter._bot_message_ts = set()
        adapter._BOT_TS_MAX = 5000
        client = SimpleNamespace(chat_postMessage=AsyncMock(side_effect=post_result))
        adapter._get_client = lambda chat_id: client
        # send() would consume a pending slash-command response_url and answer
        # ephemerally (no ts); the thread root must never go through it.
        adapter.send = AsyncMock(side_effect=AssertionError("must not use send()"))
        return adapter, client

    @pytest.mark.asyncio
    async def test_slack_root_message_becomes_the_thread(self):
        adapter, client = self._slack([{"ts": "1700.01"}])
        src = _source(Platform.SLACK, chat_id="C1", thread_id="1600.00")

        thread = await adapter.open_branch_thread(src, "Idea", "⑂ intro")

        kwargs = client.chat_postMessage.await_args.kwargs
        assert kwargs["channel"] == "C1"
        assert "thread_ts" not in kwargs  # top-level message, not a reply
        assert (thread.chat_id, thread.chat_type, thread.thread_id) == ("C1", "group", "1700.01")
        assert "1700.01" in adapter._bot_message_ts  # replies there get answered

    @pytest.mark.asyncio
    async def test_slack_post_failure_returns_none(self):
        adapter, _client = self._slack(RuntimeError("not_in_channel"))

        assert await adapter.open_branch_thread(_source(Platform.SLACK), "Idea", "x") is None

    @pytest.mark.asyncio
    async def test_matrix_root_event_becomes_the_thread_and_is_tracked(self):
        from gateway.platforms.helpers import ThreadParticipationTracker
        from gateway.platforms.matrix import MatrixAdapter

        adapter = object.__new__(MatrixAdapter)
        adapter.platform = Platform.MATRIX
        adapter._threads = MagicMock(spec=ThreadParticipationTracker)
        adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="$evt"))
        src = _source(Platform.MATRIX, chat_id="!room:x")

        thread = await adapter.open_branch_thread(src, "Idea", "intro")

        assert (thread.chat_id, thread.thread_id) == ("!room:x", "$evt")
        adapter._threads.mark.assert_called_once_with("$evt")


class TestTelegram:
    def _adapter(self, bot):
        from gateway.platforms.telegram import TelegramAdapter

        adapter = object.__new__(TelegramAdapter)
        adapter.platform = Platform.TELEGRAM
        adapter.config = PlatformConfig(enabled=True, token="***")
        adapter._bot = bot
        adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="5"))
        return adapter

    @pytest.mark.asyncio
    async def test_forum_group_gets_a_new_topic(self):
        bot = SimpleNamespace(create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=77)))
        adapter = self._adapter(bot)
        src = _source(Platform.TELEGRAM, chat_id="-1001", thread_id="1")

        thread = await adapter.open_branch_thread(src, "Idea", "intro")

        bot.create_forum_topic.assert_awaited_once_with(chat_id=-1001, name="Idea")
        assert (thread.chat_id, thread.chat_type, thread.thread_id) == ("-1001", "group", "77")
        assert adapter.send.await_args.kwargs["metadata"]["thread_id"] == "77"

    @pytest.mark.asyncio
    async def test_dm_topic_intro_is_sent_as_a_new_topic_message(self):
        adapter = self._adapter(SimpleNamespace())
        adapter._create_dm_topic = AsyncMock(return_value=88)

        thread = await adapter.open_branch_thread(
            _source(Platform.TELEGRAM, chat_type="dm", chat_id="555"), "Idea", "intro"
        )

        meta = adapter.send.await_args.kwargs["metadata"]
        assert meta["thread_id"] == "88"
        # Without this exemption send() refuses an un-anchored DM topic reply.
        assert meta["telegram_dm_topic_created_for_send"] is True
        assert (thread.chat_type, thread.thread_id) == ("dm", "88")

    @pytest.mark.asyncio
    async def test_failed_intro_does_not_bind_an_empty_topic(self):
        bot = SimpleNamespace(
            create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=77)),
            delete_forum_topic=AsyncMock(),
        )
        adapter = self._adapter(bot)
        adapter.send = AsyncMock(return_value=SendResult(success=False, error="refused"))

        assert await adapter.open_branch_thread(_source(Platform.TELEGRAM, chat_id="-1001"), "Idea", "x") is None
        bot.delete_forum_topic.assert_awaited_once_with(chat_id=-1001, message_thread_id=77)

    @pytest.mark.asyncio
    async def test_non_forum_group_returns_none(self):
        bot = SimpleNamespace(create_forum_topic=AsyncMock(side_effect=Exception("chat is not a forum")))
        adapter = self._adapter(bot)

        assert await adapter.open_branch_thread(_source(Platform.TELEGRAM, chat_id="-5"), "Idea", "x") is None
        adapter.send.assert_not_called()


class TestDiscord:
    def _adapter(self, channel):
        from plugins.platforms.discord.adapter import DiscordAdapter

        adapter = object.__new__(DiscordAdapter)
        adapter.platform = Platform.DISCORD
        adapter._client = SimpleNamespace(get_channel=lambda _id: channel, fetch_channel=AsyncMock())
        adapter._threads = MagicMock()
        return adapter

    @pytest.mark.asyncio
    async def test_text_channel_gets_a_public_thread_from_a_seed_message(self):
        guild = SimpleNamespace(id=5, name="Guild")
        thread = SimpleNamespace(id=900, name="Idea", parent=None, guild=guild, topic=None)
        seed = SimpleNamespace(create_thread=AsyncMock(return_value=thread))
        channel = SimpleNamespace(id=100, name="general", guild=guild, send=AsyncMock(return_value=seed))
        thread.parent = channel
        adapter = self._adapter(channel)

        result = await adapter.open_branch_thread(_source(chat_id="100"), "Idea", "intro")

        channel.send.assert_awaited_once_with("intro")
        assert seed.create_thread.await_args.kwargs["name"] == "Idea"
        assert (result.chat_id, result.chat_type, result.thread_id) == ("900", "thread", "900")
        assert result.parent_chat_id == "100"
        adapter._threads.mark.assert_called_once_with("900")

    @pytest.mark.asyncio
    async def test_branch_from_inside_a_thread_opens_a_sibling(self):
        guild = SimpleNamespace(id=5, name="Guild")
        new_thread = SimpleNamespace(id=901, name="Idea", parent=None, guild=guild, topic=None)
        seed = SimpleNamespace(create_thread=AsyncMock(return_value=new_thread))
        parent = SimpleNamespace(id=100, name="general", guild=guild, send=AsyncMock(return_value=seed))
        new_thread.parent = parent
        current_thread = SimpleNamespace(id=900, parent=parent, guild=guild)
        adapter = self._adapter(current_thread)

        result = await adapter.open_branch_thread(
            _source(chat_type="thread", chat_id="900", thread_id="900"), "Idea", "intro"
        )

        parent.send.assert_awaited_once()
        assert result.thread_id == "901"

    @pytest.mark.asyncio
    async def test_dm_returns_none(self):
        adapter = self._adapter(None)

        assert await adapter.open_branch_thread(_source(chat_type="dm"), "Idea", "x") is None


def test_branch_in_thread_is_read_from_config_yaml(monkeypatch, tmp_path):
    from gateway.config import load_gateway_config

    (tmp_path / "config.yaml").write_text("branch_in_thread: true\n", encoding="utf-8")
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))

    assert load_gateway_config().branch_in_thread is True
    assert GatewayConfig.from_dict({}).branch_in_thread is False
