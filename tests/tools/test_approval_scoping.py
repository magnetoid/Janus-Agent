"""Scoped gateway approvals and the smart default (plan item 2.6).

* Every pending gateway approval carries a request id; a button resolves
  exactly the prompt it was attached to, and a stale id resolves nothing.
* With ``approvals.gateway_approver: requester`` (the default) only the user
  whose message triggered the command can approve or deny it.
"""
import threading
from unittest.mock import patch

import pytest

import tools.approval as approval


@pytest.fixture(autouse=True)
def _clean_queues():
    with approval._lock:
        approval._gateway_queues.clear()
    yield
    with approval._lock:
        approval._gateway_queues.clear()


@pytest.fixture()
def policy(monkeypatch):
    def _set(value):
        monkeypatch.setattr(approval, "_get_approval_config",
                            lambda: {"gateway_approver": value} if value else {})
    _set(None)
    return _set


def _enqueue(session="s1", request_id="r1", requester="alice"):
    entry = approval._ApprovalEntry({"command": "rm -rf build", "request_id": request_id,
                                     "requester": requester})
    with approval._lock:
        approval._gateway_queues.setdefault(session, []).append(entry)
    return entry


def test_request_id_resolves_exactly_that_prompt(policy):
    first = _enqueue(request_id="r1")
    second = _enqueue(request_id="r2")
    assert approval.resolve_gateway_approval("s1", "once", request_id="r2", approver="alice") == 1
    assert second.result == "once" and second.event.is_set()
    assert first.result is None and approval.has_blocking_approval("s1")


def test_stale_or_foreign_request_id_resolves_nothing(policy):
    entry = _enqueue(session="s1", request_id="r1")
    _enqueue(session="s2", request_id="r9")
    assert approval.resolve_gateway_approval("s1", "once", request_id="gone", approver="alice") == 0
    assert approval.resolve_gateway_approval("s1", "once", request_id="r9", approver="alice") == 0
    assert entry.result is None


def test_only_the_requester_can_answer_by_default(policy):
    entry = _enqueue(requester="alice")
    assert not approval.can_resolve_gateway_approval("s1", approver="bob")
    assert approval.resolve_gateway_approval("s1", "once", approver="bob") == 0
    assert approval.resolve_gateway_approval("s1", "deny", approver="bob", resolve_all=True) == 0
    assert entry.result is None
    assert approval.can_resolve_gateway_approval("s1", approver="alice")
    assert approval.resolve_gateway_approval("s1", "deny", approver="alice") == 1
    assert entry.result == "deny"


def test_resolve_all_skips_other_peoples_requests(policy):
    mine = _enqueue(request_id="a", requester="alice")
    theirs = _enqueue(request_id="b", requester="bob")
    assert approval.resolve_gateway_approval("s1", "once", approver="alice", resolve_all=True) == 1
    assert mine.result == "once" and theirs.result is None
    assert approval.has_blocking_approval("s1")


def test_any_policy_restores_the_old_behaviour(policy):
    policy("any")
    entry = _enqueue(requester="alice")
    assert approval.resolve_gateway_approval("s1", "once", approver="bob") == 1
    assert entry.result == "once"


def test_unknown_requester_or_approver_is_not_blocked(policy):
    unknown_requester = _enqueue(request_id="u", requester=None)
    assert approval.resolve_gateway_approval("s1", "once", approver="bob") == 1
    assert unknown_requester.result == "once"
    legacy = _enqueue(request_id="l", requester="alice")
    # an adapter that cannot tell who clicked passes no approver
    assert approval.resolve_gateway_approval("s1", "once") == 1
    assert legacy.result == "once"


def test_pending_request_is_stamped_with_id_hash_and_requester(policy):
    from gateway.session_context import clear_session_vars, set_session_vars
    seen = {}

    def notify(data):
        seen.update(data)
        approval.resolve_gateway_approval("s1", "deny", request_id=data["request_id"],
                                          approver="alice")

    tokens = set_session_vars(platform="telegram", chat_id="42", user_id="alice",
                              session_key="s1")
    try:
        out = approval._await_gateway_decision(
            "s1", notify, {"command": "rm -rf build", "description": "delete",
                           "pattern_key": "rm", "pattern_keys": ["rm"]})
    finally:
        clear_session_vars(tokens)
    assert out == {"resolved": True, "choice": "deny"}
    assert seen["requester"] == "alice"
    assert len(seen["request_id"]) >= 8
    assert seen["command_hash"] and seen["command_hash"] != seen["request_id"]


def test_interactive_default_is_smart_and_headless_still_denies():
    from janus_cli.config import DEFAULT_CONFIG
    with patch("janus_cli.config.load_config", return_value=DEFAULT_CONFIG):
        assert approval._get_approval_mode() == "smart"
        assert approval._get_headless_approval_mode() == "deny"
        assert approval._get_cron_approval_mode() == "deny"


def test_refusal_reasons_distinguish_stale_buttons_from_other_peoples_requests(policy):
    assert approval.gateway_approval_refusal("s1", approver="bob") is None   # nothing pending
    _enqueue(request_id="r1", requester="alice")
    assert approval.gateway_approval_refusal("s1", approver="bob", request_id="old") == "stale"
    assert approval.gateway_approval_refusal("s1", approver="bob", request_id="r1") == "not_requester"
    assert approval.gateway_approval_refusal("s1", approver="alice", request_id="r1") is None
