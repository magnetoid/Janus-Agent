"""Child list / stop / steer tools wrapping the in-process subagent registry."""

import json

from tools.delegate_tool import (
    _register_subagent,
    _unregister_subagent,
    list_children,
    steer_child,
    stop_child,
)


class _FakeChild:
    def __init__(self):
        self.interrupted = []
        self.steered = []

    def interrupt(self, message=None):
        self.interrupted.append(message)

    def steer(self, text):
        self.steered.append(text)
        return True


def test_list_stop_steer_child_roundtrip():
    child = _FakeChild()
    sid = "child-test-1"
    _register_subagent(
        {
            "subagent_id": sid,
            "parent_id": None,
            "depth": 1,
            "goal": "do a thing",
            "model": "test",
            "started_at": 0,
            "status": "running",
            "agent": child,
        }
    )
    try:
        listed = json.loads(list_children())
        assert listed["success"] is True
        assert listed["count"] >= 1
        assert any(c.get("subagent_id") == sid for c in listed["children"])
        # list_children must not leak the live agent object
        for rec in listed["children"]:
            assert "agent" not in rec

        steered = json.loads(steer_child(sid, "skip tests, just the file"))
        assert steered["success"] is True
        assert child.steered == ["skip tests, just the file"]

        stopped = json.loads(stop_child(sid))
        assert stopped["success"] is True
        assert child.interrupted
    finally:
        _unregister_subagent(sid)


def test_steer_missing_child_errors():
    raw = steer_child("no-such-child", "hello")
    data = json.loads(raw)
    assert "error" in data
    assert "no-such-child" in data["error"]
