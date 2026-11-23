"""A cron job marks only its own execution as a cron session, never the process.

The gateway and the desktop backend run the cron ticker in-process, next to
live chat sessions. The cron flag used to be written to ``os.environ`` and was
never cleared, so after the first agent job every chat turn in that process
took the cron branch of the approval gates: ``execute_code`` was hard-blocked
under the default ``approvals.cron_mode: deny`` and silently auto-approved,
with no /approve prompt, under ``cron_mode: approve``.

These tests run a job the way ``tick()`` does (a worker thread inside a copied
context) and look at what a gateway chat turn sees, after and during the job,
and at what the job itself still sees.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import contextvars
import os
import threading
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import run_job
from gateway.session_context import clear_session_vars, set_session_vars
from tools import approval as A
from tools.thread_context import propagate_context_to_thread

_CRON_BLOCK_TEXT = "Cron jobs run without a user present"
_CHAT_KEY = "agent:main:telegram:dm:1"
_JOB = {"id": "digest", "name": "digest", "prompt": "Summarise the day."}
_TIMEOUT = 30


@pytest.fixture(autouse=True)
def _gateway_process(monkeypatch):
    """A gateway process: ``JANUS_EXEC_ASK=1`` process-wide (gateway/core.py),
    manual approvals, and an empty approval store."""
    def _reset():
        with A._lock:
            A._pending.clear()
            A._session_approved.clear()
            A._permanent_approved.clear()
            A._gateway_queues.clear()
            A._gateway_notify_cbs.clear()

    _reset()
    monkeypatch.setenv("JANUS_EXEC_ASK", "1")
    for var in ("JANUS_INTERACTIVE", "JANUS_GATEWAY_SESSION", "JANUS_CRON_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    yield
    _reset()


class _OkAgent:
    """Stands in for AIAgent; subclasses probe from inside the job."""

    def __init__(self, *args, **kwargs):
        pass

    def run_conversation(self, prompt, *args, **kwargs):
        return {"final_response": "ok"}


@contextlib.contextmanager
def _offline_job_runtime(agent_cls):
    """Stub the provider, session store and agent so run_job runs offline."""
    with patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("dotenv.load_dotenv"), \
         patch("janus_state.SessionDB", return_value=MagicMock()), \
         patch(
             "janus_cli.runtime_provider.resolve_runtime_provider",
             return_value={
                 "api_key": "***",
                 "base_url": "https://example.invalid/v1",
                 "provider": "openrouter",
                 "api_mode": "chat_completions",
             },
         ), \
         patch("run_agent.AIAgent", agent_cls):
        yield


def _run_job_like_tick(job, agent_cls):
    """Run ``run_job`` the way ``tick()`` does: on a pool worker thread, inside
    a copy of the ticker's context."""
    ctx = contextvars.copy_context()
    with _offline_job_runtime(agent_cls), \
         concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, run_job, job).result(timeout=_TIMEOUT)


def _gateway_turn(fn):
    """Run *fn* the way the gateway runs a chat turn: on its own thread, with
    the message's session bound through the session contextvars."""
    out = {}

    def _turn():
        tokens = set_session_vars(platform="telegram", chat_id="1", session_key=_CHAT_KEY)
        key_token = A.set_current_session_key(_CHAT_KEY)
        try:
            out["value"] = fn()
        finally:
            A.reset_current_session_key(key_token)
            clear_session_vars(tokens)

    t = threading.Thread(target=_turn)
    t.start()
    t.join(timeout=_TIMEOUT)
    assert "value" in out, "gateway turn did not finish"
    return out["value"]


def _during_cron_job(job_probe, chat_probe):
    """Start a cron job and, while it is mid-run, call *job_probe* from inside
    the job and *chat_probe* from a concurrent gateway chat turn."""
    inside, release, seen = threading.Event(), threading.Event(), {}

    class _ProbingAgent(_OkAgent):
        def run_conversation(self, prompt, *args, **kwargs):
            try:
                seen["job"] = job_probe()
            finally:
                inside.set()
            release.wait(timeout=_TIMEOUT)
            return {"final_response": "ok"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as runner:
        job = runner.submit(_run_job_like_tick, _JOB, _ProbingAgent)
        try:
            assert inside.wait(timeout=_TIMEOUT), "cron job never reached the agent"
            chat = _gateway_turn(chat_probe)
        finally:
            release.set()
        ok, _doc, final, err = job.result(timeout=_TIMEOUT)
    assert ok and final == "ok", err
    return seen["job"], chat


def _execute_code_decision():
    return A.check_execute_code_guard("import os", "local")


def _in_tool_thread(fn):
    """Run *fn* on a tool worker thread the way agent/tool_executor.py does."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(propagate_context_to_thread(fn)).result(timeout=_TIMEOUT)


@pytest.mark.parametrize("cron_mode", ["approve", "deny"])
def test_chat_turn_after_a_cron_job_still_asks_the_user(monkeypatch, cron_mode):
    monkeypatch.setattr(A, "_get_cron_approval_mode", lambda: cron_mode)

    ok, _doc, final, err = _run_job_like_tick(_JOB, _OkAgent)
    assert ok and final == "ok", err

    is_gateway, decision = _gateway_turn(
        lambda: (A._is_gateway_approval_context(), _execute_code_decision())
    )

    # Neither a silent auto-approve (cron_mode: approve) nor the cron hard
    # block (cron_mode: deny): the chat user gets the normal approval prompt.
    assert decision["approved"] is False
    assert _CRON_BLOCK_TEXT not in (decision.get("message") or "")
    assert decision.get("status") == "pending_approval"
    assert is_gateway is True


def test_run_job_leaves_the_context_it_ran_in_unmarked(monkeypatch):
    """run_job clears the flag itself; it does not rely on tick() running it
    in a throwaway context copy."""
    monkeypatch.setattr(A, "_get_cron_approval_mode", lambda: "deny")

    def _job_then_probe():
        ok, _doc, final, err = run_job(_JOB)
        assert ok and final == "ok", err
        return _execute_code_decision()

    with _offline_job_runtime(_OkAgent), \
         concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        after = pool.submit(_job_then_probe).result(timeout=_TIMEOUT)

    assert _CRON_BLOCK_TEXT not in (after.get("message") or "")
    assert after.get("status") == "pending_approval"


def test_chat_turn_during_a_cron_job_is_not_a_cron_session(monkeypatch):
    monkeypatch.setattr(A, "_get_cron_approval_mode", lambda: "deny")

    in_job, in_chat = _during_cron_job(
        job_probe=lambda: {
            "agent_thread": _execute_code_decision(),
            "tool_thread": _in_tool_thread(_execute_code_decision),
        },
        chat_probe=_execute_code_decision,
    )

    # The job itself, and the tool threads its agent fans out to, still get
    # approvals.cron_mode.
    for where in ("agent_thread", "tool_thread"):
        assert in_job[where]["approved"] is False, where
        assert _CRON_BLOCK_TEXT in in_job[where]["message"], where
    # A chat turn running at the same time does not.
    assert in_chat["approved"] is False
    assert in_chat.get("status") == "pending_approval"
    assert _CRON_BLOCK_TEXT not in (in_chat.get("message") or "")


def test_cron_flag_reaches_the_jobs_subprocesses_only():
    from tools.environments.local import _make_run_env, _sanitize_subprocess_env

    def _subprocess_flags():
        return {
            "terminal": _make_run_env({}).get("JANUS_CRON_SESSION"),
            "background": _sanitize_subprocess_env(dict(os.environ)).get("JANUS_CRON_SESSION"),
        }

    in_job, in_chat = _during_cron_job(_subprocess_flags, _subprocess_flags)

    # A `janus` child started by the cron job knows it runs unattended ...
    assert in_job == {"terminal": "1", "background": "1"}
    # ... but nothing a concurrent chat turn starts inherits the flag.
    assert in_chat == {"terminal": None, "background": None}


def test_cron_job_counts_as_no_human_present_in_the_desktop_backend(monkeypatch):
    """The desktop backend sets JANUS_INTERACTIVE / JANUS_GATEWAY_SESSION for
    the whole process (tui_gateway ``_enable_gateway_prompts``) and also hosts
    the cron ticker. The cron job, including the background review its agent
    spawns on a bare thread, must still count as unattended for the
    agent-created-skill guard; a chat turn must not."""
    import run_agent
    import tools.skill_manager_tool as smt
    from agent import background_review

    for var in ("JANUS_INTERACTIVE", "JANUS_GATEWAY_SESSION"):
        monkeypatch.setenv(var, "1")
    monkeypatch.setattr("janus_cli.config.load_config", lambda: {})

    spawn_review = run_agent.AIAgent._spawn_background_review
    reviewed = {}
    review_done = threading.Event()

    def _fake_review(agent, messages_snapshot, prompt):
        try:
            reviewed["guard"] = smt._guard_agent_created_enabled()
        finally:
            review_done.set()

    monkeypatch.setattr(background_review, "_run_review_in_thread", _fake_review)

    def _job_probe():
        spawn_review(cast(run_agent.AIAgent, _OkAgent()), messages_snapshot=[], review_skills=True)
        assert review_done.wait(timeout=_TIMEOUT), "background review never ran"
        return {"no_human": smt._no_human_present(), "review_guard": reviewed["guard"]}

    in_job, chat_no_human = _during_cron_job(_job_probe, smt._no_human_present)

    assert in_job == {"no_human": True, "review_guard": True}
    assert chat_no_human is False
