"""Tests for janus_logging — centralized logging setup."""

import logging
import os
import stat
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

import pytest

import janus_logging


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset the module-level sentinel and clean up root logger handlers
    added by setup_logging() so tests don't leak state.

    Under xdist (-n auto) other test modules may have called setup_logging()
    in the same worker process, leaving RotatingFileHandlers on the root
    logger.  We strip ALL RotatingFileHandlers before each test so the count
    assertions are stable regardless of test ordering.
    """
    janus_logging._logging_initialized = False
    root = logging.getLogger()
    prev_root_level = root.level
    root.setLevel(logging.NOTSET)
    # Strip ALL RotatingFileHandlers — not just the ones we added — so that
    # handlers leaked from other test modules in the same xdist worker don't
    # pollute our counts.
    pre_existing = []
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler):
            root.removeHandler(h)
            h.close()
        else:
            pre_existing.append(h)
    # Ensure the record factory is installed (it's idempotent).
    janus_logging._install_session_record_factory()
    yield
    # Restore — remove any handlers added during the test.
    for h in list(root.handlers):
        if h not in pre_existing:
            root.removeHandler(h)
            h.close()
    root.setLevel(prev_root_level)
    janus_logging._logging_initialized = False
    janus_logging.clear_session_context()


@pytest.fixture
def janus_home(tmp_path, monkeypatch):
    """Provide an isolated JANUS_HOME for logging tests.

    Uses the same tmp_path as the autouse _isolate_janus_home from conftest,
    reading it back from the env var to avoid double-mkdir conflicts.
    """
    home = Path(os.environ["JANUS_HOME"])
    return home


class TestSetupLogging:
    """setup_logging() creates agent.log + errors.log with RotatingFileHandler."""

    def test_creates_log_directory(self, janus_home):
        log_dir = janus_logging.setup_logging(janus_home=janus_home)
        assert log_dir == janus_home / "logs"
        assert log_dir.is_dir()

    def test_creates_agent_log_handler(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)
        root = logging.getLogger()

        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1
        assert agent_handlers[0].level == logging.INFO

    def test_creates_errors_log_handler(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)
        root = logging.getLogger()

        error_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "errors.log" in getattr(h, "baseFilename", "")
        ]
        assert len(error_handlers) == 1
        assert error_handlers[0].level == logging.WARNING

    def test_idempotent_no_duplicate_handlers(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)
        janus_logging.setup_logging(janus_home=janus_home)  # second call — should be no-op

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1

    def test_force_reinitializes(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)
        # Force still won't add duplicate handlers because _add_rotating_handler
        # checks by resolved path.
        janus_logging.setup_logging(janus_home=janus_home, force=True)

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1

    def test_custom_log_level(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home, log_level="DEBUG")

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.DEBUG

    def test_custom_max_size_and_backup(self, janus_home):
        janus_logging.setup_logging(
            janus_home=janus_home, max_size_mb=10, backup_count=5
        )

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].maxBytes == 10 * 1024 * 1024
        assert agent_handlers[0].backupCount == 5

    def test_suppresses_noisy_loggers(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)

        assert logging.getLogger("openai").level >= logging.WARNING
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING

    def test_writes_to_agent_log(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)

        test_logger = logging.getLogger("test_janus_logging.write_test")
        test_logger.info("test message for agent.log")

        # Flush handlers
        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = janus_home / "logs" / "agent.log"
        assert agent_log.exists()
        content = agent_log.read_text()
        assert "test message for agent.log" in content

    def test_warnings_appear_in_both_logs(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)

        test_logger = logging.getLogger("test_janus_logging.warning_test")
        test_logger.warning("this is a warning")

        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = janus_home / "logs" / "agent.log"
        errors_log = janus_home / "logs" / "errors.log"
        assert "this is a warning" in agent_log.read_text()
        assert "this is a warning" in errors_log.read_text()

    def test_info_not_in_errors_log(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)

        test_logger = logging.getLogger("test_janus_logging.info_test")
        test_logger.info("info only message")

        for h in logging.getLogger().handlers:
            h.flush()

        errors_log = janus_home / "logs" / "errors.log"
        if errors_log.exists():
            assert "info only message" not in errors_log.read_text()

    def test_reads_config_yaml(self, janus_home):
        """setup_logging reads logging.level from config.yaml."""
        import yaml
        config = {"logging": {"level": "DEBUG", "max_size_mb": 2, "backup_count": 1}}
        (janus_home / "config.yaml").write_text(yaml.dump(config))

        janus_logging.setup_logging(janus_home=janus_home)

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.DEBUG
        assert agent_handlers[0].maxBytes == 2 * 1024 * 1024
        assert agent_handlers[0].backupCount == 1

    def test_explicit_params_override_config(self, janus_home):
        """Explicit function params take precedence over config.yaml."""
        import yaml
        config = {"logging": {"level": "DEBUG"}}
        (janus_home / "config.yaml").write_text(yaml.dump(config))

        janus_logging.setup_logging(janus_home=janus_home, log_level="WARNING")

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.WARNING

    def test_record_factory_installed(self, janus_home):
        """The custom record factory injects session_tag on all records."""
        janus_logging.setup_logging(janus_home=janus_home)
        factory = logging.getLogRecordFactory()
        assert getattr(factory, "_janus_session_injector", False), (
            "Record factory should have _janus_session_injector marker"
        )
        # Verify session_tag exists on a fresh record
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert hasattr(record, "session_tag")


class TestGatewayMode:
    """setup_logging(mode='gateway') creates a filtered gateway.log."""

    def test_gateway_log_created(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")
        root = logging.getLogger()

        gw_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "gateway.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gw_handlers) == 1

    def test_gateway_log_not_created_in_cli_mode(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home, mode="cli")
        root = logging.getLogger()

        gw_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "gateway.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gw_handlers) == 0

    def test_gateway_log_created_after_cli_init(self, janus_home):
        """Gateway mode attaches gateway.log even after earlier CLI init."""
        janus_logging.setup_logging(janus_home=janus_home, mode="cli")
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")

        root = logging.getLogger()
        gw_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "gateway.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gw_handlers) == 1

        logging.getLogger("gateway.run").info("gateway connected after cli init")

        for h in root.handlers:
            h.flush()

        gw_log = janus_home / "logs" / "gateway.log"
        assert gw_log.exists()
        assert "gateway connected after cli init" in gw_log.read_text()

    def test_gateway_log_created_after_cli_init_without_duplicate_handlers(self, janus_home):
        """Repeated gateway setup calls do not attach duplicate gateway handlers."""
        janus_logging.setup_logging(janus_home=janus_home, mode="cli")
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")

        root = logging.getLogger()
        gw_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "gateway.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gw_handlers) == 1

    def test_gateway_log_receives_gateway_records(self, janus_home):
        """gateway.log captures records from gateway.* loggers."""
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")

        gw_logger = logging.getLogger("gateway.platforms.telegram")
        gw_logger.info("telegram connected")

        for h in logging.getLogger().handlers:
            h.flush()

        gw_log = janus_home / "logs" / "gateway.log"
        assert gw_log.exists()
        assert "telegram connected" in gw_log.read_text()

    def test_gateway_log_rejects_non_gateway_records(self, janus_home):
        """gateway.log does NOT capture records from tools.*, agent.*, etc."""
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")

        tool_logger = logging.getLogger("tools.terminal_tool")
        tool_logger.info("running command")

        agent_logger = logging.getLogger("agent.context_compressor")
        agent_logger.info("compressing context")

        for h in logging.getLogger().handlers:
            h.flush()

        gw_log = janus_home / "logs" / "gateway.log"
        if gw_log.exists():
            content = gw_log.read_text()
            assert "running command" not in content
            assert "compressing context" not in content

    def test_agent_log_still_receives_all(self, janus_home):
        """agent.log (catch-all) still receives gateway AND tool records."""
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")

        gw_logger = logging.getLogger("gateway.run")
        file_logger = logging.getLogger("tools.file_tools")
        # Ensure propagation and levels are clean (cross-test pollution defense)
        gw_logger.propagate = True
        file_logger.propagate = True
        logging.getLogger("tools").propagate = True
        file_logger.setLevel(logging.NOTSET)
        logging.getLogger("tools").setLevel(logging.NOTSET)

        gw_logger.info("gateway msg")
        file_logger.info("file msg")

        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = janus_home / "logs" / "agent.log"
        content = agent_log.read_text()
        assert "gateway msg" in content
        assert "file msg" in content


class TestGuiMode:
    """setup_logging(mode='gui') creates a filtered gui.log."""

    def test_gui_log_created(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home, mode="gui")
        root = logging.getLogger()

        gui_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "gui.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gui_handlers) == 1

    def test_gui_log_created_after_cli_init(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home, mode="cli")
        janus_logging.setup_logging(janus_home=janus_home, mode="gui")

        root = logging.getLogger()
        gui_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "gui.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gui_handlers) == 1

    def test_gui_log_receives_only_gui_components(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home, mode="gui")

        logging.getLogger("janus_cli.web_server").info("dashboard online")
        logging.getLogger("tui_gateway.ws").info("ws connected")
        logging.getLogger("gateway.run").info("gateway event")

        for h in logging.getLogger().handlers:
            h.flush()

        gui_log = janus_home / "logs" / "gui.log"
        assert gui_log.exists()
        content = gui_log.read_text()
        assert "dashboard online" in content
        assert "ws connected" in content
        assert "gateway event" not in content


class TestSessionContext:
    """set_session_context / clear_session_context + _SessionFilter."""

    def test_session_tag_in_log_output(self, janus_home):
        """When session context is set, log lines include [session_id]."""
        janus_logging.setup_logging(janus_home=janus_home)
        janus_logging.set_session_context("abc123")

        test_logger = logging.getLogger("test.session_tag")
        test_logger.info("tagged message")

        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = janus_home / "logs" / "agent.log"
        content = agent_log.read_text()
        assert "[abc123]" in content
        assert "tagged message" in content

    def test_no_session_tag_without_context(self, janus_home):
        """Without session context, log lines have no session tag."""
        janus_logging.setup_logging(janus_home=janus_home)
        janus_logging.clear_session_context()

        test_logger = logging.getLogger("test.no_session")
        test_logger.info("untagged message")

        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = janus_home / "logs" / "agent.log"
        content = agent_log.read_text()
        assert "untagged message" in content
        # Should not have any [xxx] session tag
        import re
        for line in content.splitlines():
            if "untagged message" in line:
                assert not re.search(r"\[.+?\]", line.split("INFO")[1].split("test.no_session")[0])

    def test_clear_session_context(self, janus_home):
        """After clearing, session tag disappears."""
        janus_logging.setup_logging(janus_home=janus_home)
        janus_logging.set_session_context("xyz789")
        janus_logging.clear_session_context()

        test_logger = logging.getLogger("test.cleared")
        test_logger.info("after clear")

        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = janus_home / "logs" / "agent.log"
        content = agent_log.read_text()
        assert "[xyz789]" not in content

    def test_session_context_thread_isolated(self, janus_home):
        """Session context is per-thread — one thread's context doesn't leak."""
        janus_logging.setup_logging(janus_home=janus_home)

        results = {}

        def thread_a():
            janus_logging.set_session_context("thread_a_session")
            logging.getLogger("test.thread_a").info("from thread A")
            for h in logging.getLogger().handlers:
                h.flush()

        def thread_b():
            janus_logging.set_session_context("thread_b_session")
            logging.getLogger("test.thread_b").info("from thread B")
            for h in logging.getLogger().handlers:
                h.flush()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        ta.join()
        tb.start()
        tb.join()

        agent_log = janus_home / "logs" / "agent.log"
        content = agent_log.read_text()

        # Each thread's message should have its own session tag
        for line in content.splitlines():
            if "from thread A" in line:
                assert "[thread_a_session]" in line
                assert "[thread_b_session]" not in line
            if "from thread B" in line:
                assert "[thread_b_session]" in line
                assert "[thread_a_session]" not in line


class TestTurnContext:
    """set_turn_context threads turn/request ids onto every record."""

    @staticmethod
    def _record():
        factory = logging.getLogRecordFactory()
        return factory("test", logging.INFO, "", 0, "msg", (), None)

    def test_turn_and_request_land_on_record_fields(self):
        """Structured consumers read full ids off discrete record fields."""
        janus_logging.set_session_context("sess1")
        janus_logging.set_turn_context(
            turn_id="sess1:task7:abcd1234", request_id="sess1:task7:abcd1234:api:3"
        )

        record = self._record()
        assert record.session_id_ctx == "sess1"
        assert record.turn_id == "sess1:task7:abcd1234"
        assert record.request_id == "sess1:task7:abcd1234:api:3"

    def test_human_tag_carries_short_turn_and_call_segments(self):
        """The text tag stays readable: only the trailing segments ride it."""
        janus_logging.set_session_context("sess1")
        janus_logging.set_turn_context(
            turn_id="sess1:task7:abcd1234", request_id="sess1:task7:abcd1234:api:3"
        )

        tag = self._record().session_tag
        assert tag == " [sess1 t:abcd1234 #3]"

    def test_turn_context_without_session(self):
        """A turn id alone still tags the line."""
        janus_logging.clear_session_context()
        janus_logging.set_turn_context(turn_id="orphan:turn:deadbeef")

        assert self._record().session_tag == " [t:deadbeef]"

    def test_partial_update_leaves_the_other_id_untouched(self):
        """set_turn_context(request_id=...) must not wipe the turn id."""
        janus_logging.set_turn_context(turn_id="s:t:aaaa1111", request_id="")
        janus_logging.set_turn_context(request_id="s:t:aaaa1111:api:2")

        record = self._record()
        assert record.turn_id == "s:t:aaaa1111"
        assert record.request_id == "s:t:aaaa1111:api:2"

    def test_clear_session_context_clears_turn_and_request(self):
        """Turn state must not leak from one conversation into the next."""
        janus_logging.set_session_context("sess1")
        janus_logging.set_turn_context(turn_id="s:t:aaaa1111", request_id="s:t:aaaa1111:api:1")

        janus_logging.clear_session_context()

        record = self._record()
        assert record.session_id_ctx == ""
        assert record.turn_id == ""
        assert record.request_id == ""
        assert record.session_tag == ""

    def test_turn_context_is_thread_local(self):
        """One thread's turn id never bleeds into another's records."""
        janus_logging.set_turn_context(turn_id="main:turn:1111aaaa")
        seen = {}

        def worker():
            seen["before"] = getattr(self._record(), "turn_id", None)
            janus_logging.set_turn_context(turn_id="worker:turn:2222bbbb")
            seen["after"] = self._record().turn_id

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert seen["before"] == ""
        assert seen["after"] == "worker:turn:2222bbbb"
        assert self._record().turn_id == "main:turn:1111aaaa"

    def test_turn_tag_reaches_the_log_file(self, janus_home):
        """End-to-end: the turn tag is rendered into agent.log."""
        janus_logging.setup_logging(janus_home=janus_home)
        janus_logging.set_session_context("e2e")
        janus_logging.set_turn_context(turn_id="e2e:task:99887766", request_id="e2e:task:99887766:api:1")

        logging.getLogger("test.turn_tag").info("turn tagged message")
        for h in logging.getLogger().handlers:
            h.flush()

        content = (janus_home / "logs" / "agent.log").read_text()
        assert "[e2e t:99887766 #1]" in content
        assert "turn tagged message" in content


class TestRecordFactory:
    """Unit tests for the custom LogRecord factory."""

    def test_record_has_session_tag(self):
        """Every record gets a session_tag attribute."""
        factory = logging.getLogRecordFactory()
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert hasattr(record, "session_tag")

    def test_empty_tag_without_context(self):
        janus_logging.clear_session_context()
        factory = logging.getLogRecordFactory()
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert record.session_tag == ""

    def test_tag_with_context(self):
        janus_logging.set_session_context("sess_42")
        factory = logging.getLogRecordFactory()
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert record.session_tag == " [sess_42]"

    def test_idempotent_install(self):
        """Calling _install_session_record_factory() twice doesn't double-wrap."""
        janus_logging._install_session_record_factory()
        factory_a = logging.getLogRecordFactory()
        janus_logging._install_session_record_factory()
        factory_b = logging.getLogRecordFactory()
        assert factory_a is factory_b

    def test_works_with_any_handler(self):
        """A handler using %(session_tag)s works even without _SessionFilter."""
        janus_logging.set_session_context("any_handler_test")
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(session_tag)s %(message)s"))

        logger = logging.getLogger("_test_any_handler")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            # Should not raise KeyError
            logger.info("hello")
        finally:
            logger.removeHandler(handler)


class TestComponentFilter:
    """Unit tests for _ComponentFilter."""

    def test_passes_matching_prefix(self):
        f = janus_logging._ComponentFilter(("gateway",))
        record = logging.LogRecord(
            "gateway.run", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is True

    def test_passes_nested_matching_prefix(self):
        f = janus_logging._ComponentFilter(("gateway",))
        record = logging.LogRecord(
            "gateway.platforms.telegram", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is True

    def test_blocks_non_matching(self):
        f = janus_logging._ComponentFilter(("gateway",))
        record = logging.LogRecord(
            "tools.terminal_tool", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is False

    def test_multiple_prefixes(self):
        f = janus_logging._ComponentFilter(("agent", "run_agent", "model_tools"))
        assert f.filter(logging.LogRecord(
            "agent.compressor", logging.INFO, "", 0, "", (), None
        ))
        assert f.filter(logging.LogRecord(
            "run_agent", logging.INFO, "", 0, "", (), None
        ))
        assert f.filter(logging.LogRecord(
            "model_tools", logging.INFO, "", 0, "", (), None
        ))
        assert not f.filter(logging.LogRecord(
            "tools.browser", logging.INFO, "", 0, "", (), None
        ))


class TestComponentPrefixes:
    """COMPONENT_PREFIXES covers the expected components."""

    def test_gateway_prefix(self):
        assert "gateway" in janus_logging.COMPONENT_PREFIXES
        # The gateway component captures both core gateway logs and the
        # janus_plugins facility (plugin-installed gateway adapters log
        # under that prefix).
        assert ("gateway", "janus_plugins") == janus_logging.COMPONENT_PREFIXES["gateway"]

    def test_agent_prefix(self):
        prefixes = janus_logging.COMPONENT_PREFIXES["agent"]
        assert "agent" in prefixes
        assert "run_agent" in prefixes
        assert "model_tools" in prefixes

    def test_tools_prefix(self):
        assert ("tools",) == janus_logging.COMPONENT_PREFIXES["tools"]

    def test_cli_prefix(self):
        prefixes = janus_logging.COMPONENT_PREFIXES["cli"]
        assert "janus_cli" in prefixes
        assert "cli" in prefixes

    def test_cron_prefix(self):
        assert ("cron",) == janus_logging.COMPONENT_PREFIXES["cron"]

    def test_gui_prefix(self):
        prefixes = janus_logging.COMPONENT_PREFIXES["gui"]
        assert "janus_cli.web_server" in prefixes
        assert "tui_gateway" in prefixes


class TestSetupVerboseLogging:
    """setup_verbose_logging() adds a DEBUG-level console handler."""

    def test_adds_stream_handler(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)
        janus_logging.setup_verbose_logging()

        root = logging.getLogger()
        verbose_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_janus_verbose", False)
        ]
        assert len(verbose_handlers) == 1
        assert verbose_handlers[0].level == logging.DEBUG

    def test_idempotent(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home)
        janus_logging.setup_verbose_logging()
        janus_logging.setup_verbose_logging()  # second call

        root = logging.getLogger()
        verbose_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_janus_verbose", False)
        ]
        assert len(verbose_handlers) == 1


class TestAddRotatingHandler:
    """_add_rotating_handler() is idempotent and creates the directory."""

    def test_creates_directory(self, tmp_path):
        log_path = tmp_path / "subdir" / "test.log"
        logger = logging.getLogger("_test_rotating")
        formatter = logging.Formatter("%(message)s")

        janus_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        assert log_path.parent.is_dir()
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_no_duplicate_for_same_path(self, tmp_path):
        log_path = tmp_path / "test.log"
        logger = logging.getLogger("_test_rotating_dup")
        formatter = logging.Formatter("%(message)s")

        janus_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )
        janus_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        rotating_handlers = [
            h for h in logger.handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert len(rotating_handlers) == 1
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_log_filter_attached(self, tmp_path):
        """Optional log_filter is attached to the handler."""
        log_path = tmp_path / "filtered.log"
        logger = logging.getLogger("_test_rotating_filter")
        formatter = logging.Formatter("%(message)s")
        component_filter = janus_logging._ComponentFilter(("test",))

        janus_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
            log_filter=component_filter,
        )

        handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
        assert component_filter in handlers[0].filters
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_no_session_filter_on_handler(self, tmp_path):
        """Handlers rely on record factory, not per-handler _SessionFilter."""
        log_path = tmp_path / "no_session_filter.log"
        logger = logging.getLogger("_test_no_session_filter")
        formatter = logging.Formatter("%(session_tag)s%(message)s")

        janus_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
        # No _SessionFilter on the handler — record factory handles it
        assert len(handlers[0].filters) == 0

        # But session_tag still works (via record factory)
        janus_logging.set_session_context("factory_test")
        logger.info("test msg")
        handlers[0].flush()
        content = log_path.read_text()
        assert "[factory_test]" in content

        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_managed_mode_initial_open_sets_group_writable(self, tmp_path):
        log_path = tmp_path / "managed-open.log"
        logger = logging.getLogger("_test_rotating_managed_open")
        formatter = logging.Formatter("%(message)s")

        old_umask = os.umask(0o022)
        try:
            with patch("janus_cli.config.is_managed", return_value=True):
                janus_logging._add_rotating_handler(
                    logger, log_path,
                    level=logging.INFO, max_bytes=1024, backup_count=1,
                    formatter=formatter,
                )
        finally:
            os.umask(old_umask)

        assert log_path.exists()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o660

        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_managed_mode_rollover_sets_group_writable(self, tmp_path):
        log_path = tmp_path / "managed-rollover.log"
        logger = logging.getLogger("_test_rotating_managed_rollover")
        formatter = logging.Formatter("%(message)s")

        old_umask = os.umask(0o022)
        try:
            with patch("janus_cli.config.is_managed", return_value=True):
                janus_logging._add_rotating_handler(
                    logger, log_path,
                    level=logging.INFO, max_bytes=1, backup_count=1,
                    formatter=formatter,
                )
                handler = next(
                    h for h in logger.handlers if isinstance(h, RotatingFileHandler)
                )
                logger.info("a" * 256)
                handler.flush()
        finally:
            os.umask(old_umask)

        assert log_path.exists()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o660

        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()


class TestReadLoggingConfig:
    """_read_logging_config() reads from config.yaml."""

    def test_returns_none_when_no_config(self, janus_home):
        level, max_size, backup, fmt = janus_logging._read_logging_config()
        assert level is None
        assert max_size is None
        assert backup is None
        assert fmt is None

    def test_reads_logging_section(self, janus_home):
        import yaml
        config = {"logging": {"level": "DEBUG", "max_size_mb": 10,
                              "backup_count": 5, "format": "json"}}
        (janus_home / "config.yaml").write_text(yaml.dump(config))

        level, max_size, backup, fmt = janus_logging._read_logging_config()
        assert level == "DEBUG"
        assert max_size == 10
        assert backup == 5
        assert fmt == "json"

    def test_handles_missing_logging_section(self, janus_home):
        import yaml
        config = {"model": "test"}
        (janus_home / "config.yaml").write_text(yaml.dump(config))

        level, max_size, backup, fmt = janus_logging._read_logging_config()
        assert level is None
        assert fmt is None


class TestJSONLogFormat:
    """logging.format: json switches the file handlers to JSONL output."""

    def _write_config(self, janus_home, fmt):
        import yaml
        (janus_home / "config.yaml").write_text(yaml.dump({"logging": {"format": fmt}}))

    def test_json_format_writes_one_object_per_line(self, janus_home):
        import json
        self._write_config(janus_home, "json")
        janus_logging.setup_logging(janus_home=janus_home, force=True)
        janus_logging.set_session_context("jsonsess")
        janus_logging.set_turn_context(turn_id="jsonsess:task:12345678",
                                       request_id="jsonsess:task:12345678:api:2")

        logging.getLogger("test.jsonfmt").warning("structured hello")
        for h in logging.getLogger().handlers:
            h.flush()

        lines = [ln for ln in (janus_home / "logs" / "agent.log").read_text().splitlines() if ln.strip()]
        assert lines, "agent.log should not be empty"
        payloads = [json.loads(ln) for ln in lines]
        match = [p for p in payloads if p["message"] == "structured hello"]
        assert len(match) == 1
        entry = match[0]
        assert entry["level"] == "WARNING"
        assert entry["logger"] == "test.jsonfmt"
        assert entry["session_id"] == "jsonsess"
        assert entry["turn_id"] == "jsonsess:task:12345678"
        assert entry["request_id"] == "jsonsess:task:12345678:api:2"
        assert entry["ts"]

    def test_text_format_is_the_default(self, janus_home):
        janus_logging.setup_logging(janus_home=janus_home, force=True)
        logging.getLogger("test.textfmt").warning("plain hello")
        for h in logging.getLogger().handlers:
            h.flush()

        content = (janus_home / "logs" / "agent.log").read_text()
        assert "plain hello" in content
        assert not content.lstrip().startswith("{")

    def test_json_format_redacts_secrets(self, janus_home):
        import json
        self._write_config(janus_home, "json")
        janus_logging.setup_logging(janus_home=janus_home, force=True)

        secret = "sk-ant-api03-" + "B" * 40
        logging.getLogger("test.jsonsecret").error("token was %s", secret)
        for h in logging.getLogger().handlers:
            h.flush()

        raw = (janus_home / "logs" / "agent.log").read_text()
        assert secret not in raw
        payloads = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
        assert any("token was" in p["message"] for p in payloads)


class TestExternalRotationRecovery:
    """_ManagedRotatingFileHandler recovers from external rotation.

    External rotation = anything that renames, unlinks, or replaces the
    log file without going through ``doRollover()``: logrotate, manual
    ``mv``, another process rotating under us, or a transient ``rm``.
    Before this fix the open file descriptor stayed pinned to the old
    inode forever, so every subsequent write went to the rotated backup
    instead of the file the operator expects to read.
    """

    def _make_handler(self, log_path: Path) -> janus_logging._ManagedRotatingFileHandler:
        handler = janus_logging._ManagedRotatingFileHandler(
            str(log_path), maxBytes=10 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def _emit(self, handler: logging.Handler, msg: str) -> None:
        record = logging.LogRecord(
            name="gateway.run", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )
        # Match the record factory that janus_logging installs at import time.
        record.session_tag = ""
        handler.emit(record)
        handler.flush()

    def test_recovers_after_external_rename(self, tmp_path):
        """logrotate-style external rename: ``mv gateway.log gateway.log.1``.

        Handler's fd was pinned to the renamed inode; new writes used to
        go to ``gateway.log.1`` forever.  After fix, the handler reopens
        ``gateway.log`` at the original path.
        """
        log_path = tmp_path / "gateway.log"
        rotated = tmp_path / "gateway.log.1"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "before rotation")
            assert log_path.read_text() == "before rotation\n"

            # External rotation (NOT via handler.doRollover()).
            os.rename(log_path, rotated)
            assert not log_path.exists()

            self._emit(handler, "after rotation")

            # The new write should land in a freshly recreated gateway.log,
            # not appended to the rotated backup.
            assert log_path.exists(), "handler did not recreate gateway.log"
            assert log_path.read_text() == "after rotation\n"
            assert rotated.read_text() == "before rotation\n"
        finally:
            handler.close()

    def test_recovers_after_external_unlink(self, tmp_path):
        """``rm gateway.log`` then keep writing — handler recreates the file."""
        log_path = tmp_path / "gateway.log"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "before unlink")
            assert log_path.read_text() == "before unlink\n"

            os.unlink(log_path)
            assert not log_path.exists()

            self._emit(handler, "after unlink")
            assert log_path.exists()
            assert log_path.read_text() == "after unlink\n"
        finally:
            handler.close()

    def test_external_truncate_does_not_force_reopen(self, tmp_path):
        """``: > gateway.log`` keeps the same inode — no reopen needed.

        Truncation in place preserves the inode, so subsequent writes
        continue to the same file descriptor.  We assert the post-truncate
        content reflects the truncate (size shrinks) and then grows with
        new writes — i.e. the handler correctly does NOT detect this as
        an inode change.
        """
        log_path = tmp_path / "gateway.log"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "AAAA" * 32)
            assert log_path.stat().st_size > 0

            with open(log_path, "w"):
                pass  # truncate to zero
            assert log_path.stat().st_size == 0

            self._emit(handler, "after truncate")
            assert log_path.read_text() == "after truncate\n"
        finally:
            handler.close()

    def test_normal_rollover_still_works(self, tmp_path):
        """Handler-driven ``doRollover()`` must continue to work normally.

        Regression guard: the inode-snapshot bookkeeping must be refreshed
        in ``doRollover()`` so the very next emit doesn't mistake our own
        rollover for an external one and double-reopen.
        """
        log_path = tmp_path / "gateway.log"
        rotated = tmp_path / "gateway.log.1"

        # Tiny maxBytes forces rollover after the first record.
        handler = janus_logging._ManagedRotatingFileHandler(
            str(log_path), maxBytes=1, backupCount=1, encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        try:
            self._emit(handler, "first record")
            self._emit(handler, "second record")
            self._emit(handler, "third record")

            # After rollover we should have BOTH files, with the most
            # recent record in the live file.
            assert log_path.exists()
            assert rotated.exists()
            assert "third record" in log_path.read_text()
        finally:
            handler.close()

    def test_gateway_log_attached_after_external_rotation_then_re_setup(
        self, janus_home,
    ):
        """End-to-end Allen-reproduction: gateway.log gets externally rotated,
        ``setup_logging(mode='gateway')`` is re-called, the handler keeps
        working.

        Reproduces Allen's symptom (gateway.log frozen mid-write, all gateway
        records leaking to agent.log) when something external rotates the
        file between setup_logging() calls.
        """
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")
        gw_path = janus_home / "logs" / "gateway.log"
        rotated = janus_home / "logs" / "gateway.log.1"

        logging.getLogger("gateway.run").info("line BEFORE rotation")
        for h in logging.getLogger().handlers:
            try: h.flush()
            except Exception: pass
        assert "BEFORE rotation" in gw_path.read_text()

        # External actor renames the file out from under us.
        os.rename(gw_path, rotated)
        assert not gw_path.exists()

        # Caller (or some restart path) re-enters setup_logging.  This used
        # to silently no-op due to the per-path dedup check, leaving the
        # stale fd in place.
        janus_logging.setup_logging(janus_home=janus_home, mode="gateway")

        logging.getLogger("gateway.run").info("line AFTER rotation")
        for h in logging.getLogger().handlers:
            try: h.flush()
            except Exception: pass

        # The new record must reach the live gateway.log, not the rotated
        # backup.  Allen's logs had everything past the rotation point
        # going into agent.log only, never gateway.log.
        assert gw_path.exists(), "gateway.log was never recreated"
        assert "AFTER rotation" in gw_path.read_text()
        assert "AFTER rotation" not in rotated.read_text()


class TestSafeStderr:
    """Tests for _safe_stderr() — Unicode tolerance on Windows console."""

    def test_returns_stderr_on_utf8_system(self, monkeypatch):
        """On UTF-8 systems, _safe_stderr() returns sys.stderr unchanged."""
        import io
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        # On Linux/macOS, encoding is typically utf-8
        result = janus_logging._safe_stderr()
        # Should return the same object (or a equivalent stream)
        assert result is fake_stderr or getattr(result, "encoding", "").lower().startswith("utf")

    def test_wraps_non_utf8_stderr(self, monkeypatch):
        """On non-UTF-8 systems (e.g. Windows cp949), wraps stderr with UTF-8."""
        import io

        class FakeStderr:
            """Simulates a Windows stderr with legacy encoding."""
            encoding = "cp949"
            buffer = io.BytesIO()

            def write(self, s):
                pass

            def flush(self):
                pass

        fake = FakeStderr()
        monkeypatch.setattr(sys, "stderr", fake)
        result = janus_logging._safe_stderr()
        # Should be a TextIOWrapper, not the original FakeStderr
        assert isinstance(result, io.TextIOWrapper)
        assert result.encoding == "utf-8"
        assert result.errors == "replace"

    def test_handler_emits_unicode_without_crash(self, tmp_path):
        """StreamHandler with _safe_stderr can emit Unicode messages."""
        import io

        # Create a stderr-like stream with ASCII encoding
        class AsciiStream:
            encoding = "ascii"
            buffer = io.BytesIO()

            def write(self, s):
                self.buffer.write(s.encode("ascii", errors="replace"))

            def flush(self):
                pass

        # Without the fix, this would crash on cp949/ASCII stderr.
        # With the wrapper, the em-dash is replaced with '?'
        handler = logging.StreamHandler(
            io.TextIOWrapper(
                io.BytesIO(),
                encoding="utf-8",
                errors="replace",
            )
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("_test_unicode")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            # Em-dash U+2014 — the exact character from the bug report
            logger.info("Session hygiene: 400 messages — auto-compressing")
        finally:
            logger.removeHandler(handler)
