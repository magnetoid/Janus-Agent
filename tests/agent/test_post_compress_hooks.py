"""`on_post_compress` for memory providers and the `post_compress` plugin hook (plan item 3.6)."""
import os
from unittest.mock import MagicMock, patch

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _Recorder(MemoryProvider):
    def __init__(self):
        self.calls = []

    @property
    def name(self):
        return "recorder"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def on_post_compress(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs))


def test_default_provider_hook_is_a_noop():
    class _Plain(_Recorder):
        on_post_compress = MemoryProvider.on_post_compress
    assert _Plain().on_post_compress([{"role": "user", "content": "x"}]) is None


def test_manager_fans_out_and_isolates_failures():
    good, bad = _Recorder(), _Recorder()
    bad.on_post_compress = MagicMock(side_effect=RuntimeError("boom"))
    mgr = MemoryManager()
    mgr._providers = [bad, good]
    mgr.on_post_compress([{"role": "user", "content": "s"}], old_session_id="a", new_session_id="b")
    assert good.calls and good.calls[0][1]["old_session_id"] == "a"


def test_post_compress_is_a_valid_plugin_hook():
    from janus_cli.plugins import VALID_HOOKS
    assert "post_compress" in VALID_HOOKS


def test_compaction_notifies_providers_and_plugins_after_compressing(tmp_path):
    from janus_state import SessionDB
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        model="test/model", quiet_mode=True, skip_context_files=True,
                        skip_memory=True, session_db=SessionDB(db_path=tmp_path / "s.db"),
                        session_id="before")
    compressed = [{"role": "user", "content": "[CONTEXT COMPACTION] s"}]
    compressor = MagicMock()
    compressor.compress.return_value = compressed
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    recorder = _Recorder()
    manager = MemoryManager()
    manager._providers = [recorder]
    agent._memory_manager = manager

    with patch("janus_cli.plugins.invoke_hook") as hook:
        agent._compress_context([{"role": "user", "content": f"m{i}"} for i in range(6)],
                                "sys", approx_tokens=10_000)

    assert len(recorder.calls) == 1
    msgs, kwargs = recorder.calls[0]
    assert any(str(m.get("content", "")).startswith("[CONTEXT COMPACTION]") for m in msgs)
    assert kwargs["old_session_id"] == "before" and kwargs["new_session_id"] == agent.session_id
    names = [c.args[0] for c in hook.call_args_list]
    assert "post_compress" in names
    kw = hook.call_args_list[names.index("post_compress")].kwargs
    assert kw["messages_before"] == 6 and kw["old_session_id"] == "before"
