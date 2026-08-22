"""A broken UI callback must leave a trace in the log.

The stream/reasoning/tool-gen fan-out helpers deliberately swallow callback
exceptions — a broken display hook must never kill the agent turn.  But
swallowing silently means a UI that drops every visible token looks
identical to a model that produced nothing.  These tests pin both halves of
that contract: the exception is still swallowed, AND it is recorded at DEBUG
with a traceback.
"""

import logging

import pytest

from run_agent import AIAgent


def _bare_agent(**attrs):
    """An AIAgent with only the attributes the fan-out helpers read.

    AIAgent.__init__ takes ~60 params and builds real clients; these helpers
    touch a handful of attributes, so construct the object directly.
    """
    agent = object.__new__(AIAgent)
    agent.stream_delta_callback = None
    agent._stream_callback = None
    agent.reasoning_callback = None
    agent.tool_gen_callback = None
    agent._stream_think_scrubber = None
    agent._stream_context_scrubber = None
    agent._stream_needs_break = False
    agent._current_streamed_assistant_text = ""
    for key, value in attrs.items():
        setattr(agent, key, value)
    return agent


def _boom(*args, **kwargs):
    raise RuntimeError("callback exploded")


class TestStreamDeltaCallbackErrors:
    def test_broken_callback_does_not_raise(self):
        agent = _bare_agent(stream_delta_callback=_boom)
        agent._fire_stream_delta("visible text")  # must not raise

    def test_broken_callback_is_logged_with_traceback(self, caplog):
        agent = _bare_agent(stream_delta_callback=_boom)
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            agent._fire_stream_delta("visible text")

        records = [r for r in caplog.records if "_fire_stream_delta" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        assert records[0].exc_info is not None
        assert "callback exploded" in caplog.text

    def test_working_callback_logs_nothing(self, caplog):
        seen = []
        agent = _bare_agent(stream_delta_callback=seen.append)
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            agent._fire_stream_delta("visible text")

        assert seen == ["visible text"]
        assert not [r for r in caplog.records if "_fire_stream_delta" in r.getMessage()]

    def test_text_is_not_recorded_as_streamed_when_every_callback_fails(self):
        """The `delivered` gate must survive the logging change."""
        agent = _bare_agent(stream_delta_callback=_boom)
        agent._fire_stream_delta("visible text")
        assert agent._current_streamed_assistant_text == ""

    def test_one_failing_callback_does_not_block_the_other(self, caplog):
        seen = []
        agent = _bare_agent(stream_delta_callback=_boom, _stream_callback=seen.append)
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            agent._fire_stream_delta("visible text")

        assert seen == ["visible text"]
        assert agent._current_streamed_assistant_text == "visible text"
        assert len([r for r in caplog.records if "_fire_stream_delta" in r.getMessage()]) == 1


class TestReasoningCallbackErrors:
    def test_broken_callback_does_not_raise(self):
        _bare_agent(reasoning_callback=_boom)._fire_reasoning_delta("thinking")

    def test_broken_callback_is_logged_with_traceback(self, caplog):
        agent = _bare_agent(reasoning_callback=_boom)
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            agent._fire_reasoning_delta("thinking")

        records = [r for r in caplog.records if "_fire_reasoning_delta" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None


class TestToolGenCallbackErrors:
    def test_broken_callback_does_not_raise(self):
        _bare_agent(tool_gen_callback=_boom)._fire_tool_gen_started("write_file")

    def test_broken_callback_is_logged_with_traceback(self, caplog):
        agent = _bare_agent(tool_gen_callback=_boom)
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            agent._fire_tool_gen_started("write_file")

        records = [r for r in caplog.records if "_fire_tool_gen_started" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None


class _FlushingScrubber:
    """Minimal scrubber stub whose flush() yields a held-back tail."""

    def __init__(self, tail=""):
        self._tail = tail

    def feed(self, text):
        return text

    def flush(self):
        tail, self._tail = self._tail, ""
        return tail


class TestScrubberTailFlushErrors:
    @pytest.mark.parametrize("scrubber_attr", ["_stream_think_scrubber", "_stream_context_scrubber"])
    def test_broken_callback_on_tail_flush_is_logged(self, caplog, scrubber_attr):
        agent = _bare_agent(stream_delta_callback=_boom)
        setattr(agent, scrubber_attr, _FlushingScrubber("held-back tail"))

        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            agent._reset_stream_delivery_tracking()  # must not raise

        records = [
            r for r in caplog.records
            if "_reset_stream_delivery_tracking" in r.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None
