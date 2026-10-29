"""Approval buttons honour requester binding on every adapter (backlog F1).

``resolve_gateway_approval`` only enforces ``approvals.gateway_approver:
requester`` when the adapter passes who clicked. These tests drive each
adapter's click handler against the real approval queue: a click from
someone other than the requester must leave the prompt pending (and its
buttons usable), and the requester's click must resolve it.
"""

import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _approval_state():
    from tools.approval import _gateway_queues
    _gateway_queues.clear()
    with patch("tools.approval._get_approval_config", return_value={}):
        yield
    _gateway_queues.clear()


def _pending(session_key, requester, request_id="req-1"):
    from tools.approval import _ApprovalEntry, _gateway_queues
    entry = _ApprovalEntry({"command": "rm -rf build", "request_id": request_id,
                            "requester": requester})
    _gateway_queues[session_key] = [entry]
    return entry


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _discord_view(session_key, request_id="req-1"):
    from plugins.platforms.discord.adapter import ExecApprovalView
    return ExecApprovalView(session_key=session_key, allowed_user_ids={"111", "222"},
                            request_id=request_id)


def _discord_interaction(user_id):
    response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
    message = SimpleNamespace(embeds=[])
    user = SimpleNamespace(id=int(user_id), display_name=f"user{user_id}", roles=[])
    return SimpleNamespace(user=user, response=response, message=message)


@pytest.mark.asyncio
async def test_discord_click_from_someone_else_is_refused_and_keeps_buttons():
    entry = _pending("discord-sess", requester="111")
    view = _discord_view("discord-sess")
    interaction = _discord_interaction("222")

    await view._resolve(interaction, "once", MagicMock(), "Approved once")

    assert not entry.event.is_set()
    assert view.resolved is False
    assert not any(getattr(child, "disabled", False) for child in view.children)
    interaction.response.edit_message.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_requester_click_resolves():
    entry = _pending("discord-sess", requester="111")
    view = _discord_view("discord-sess")

    await view._resolve(_discord_interaction("111"), "session", MagicMock(), "Approved")

    assert entry.event.is_set() and entry.result == "session"
    assert view.resolved is True


@pytest.mark.asyncio
async def test_discord_stale_button_resolves_nothing():
    entry = _pending("discord-sess", requester="111", request_id="req-new")
    view = _discord_view("discord-sess", request_id="req-old")

    await view._resolve(_discord_interaction("111"), "once", MagicMock(), "Approved")

    assert not entry.event.is_set()


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def _teams_ctx(clicker_id, data):
    from_account = SimpleNamespace(aad_object_id=clicker_id, id=f"29:{clicker_id}")
    action = SimpleNamespace(data=data)
    activity = SimpleNamespace(value=SimpleNamespace(action=action), from_=from_account)
    return SimpleNamespace(activity=activity)


def _teams_adapter():
    from tests.gateway.test_teams import TeamsAdapter, _make_config
    return TeamsAdapter(_make_config(client_id="id", client_secret="s", tenant_id="t"))


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_all", [False, True])
async def test_teams_click_from_someone_else_is_refused(monkeypatch, allow_all):
    if allow_all:
        monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
    else:
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "aad-a,aad-b")
    entry = _pending("teams-sess", requester="aad-a")
    data = {"session_key": "teams-sess", "janus_action": "approve_once",
            "request_id": "req-1", "cmd": "rm -rf build"}

    await _teams_adapter()._on_card_action(_teams_ctx("aad-b", data))
    assert not entry.event.is_set()

    await _teams_adapter()._on_card_action(_teams_ctx("aad-a", data))
    assert entry.event.is_set() and entry.result == "once"


@pytest.mark.asyncio
async def test_teams_prompt_carries_the_request_id(monkeypatch):
    adapter = _teams_adapter()
    adapter._app = MagicMock()
    adapter._send_card = AsyncMock(return_value=SimpleNamespace(id="m1"))
    seen = []
    monkeypatch.setattr(sys.modules[type(adapter).__module__], "ExecuteAction",
                        lambda **kw: seen.append(kw) or kw)

    await adapter.send_exec_approval("chat", "rm -rf build", "teams-sess",
                                     metadata={"approval_request_id": "req-9"})

    assert seen and all(kw["data"].get("request_id") == "req-9" for kw in seen)


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------

def _feishu_adapter():
    from tests.gateway.test_feishu_approval_buttons import _make_adapter
    adapter = _make_adapter()
    adapter._is_interactive_operator_authorized = lambda _open_id: True
    adapter._approval_state[7] = {"session_key": "feishu-sess", "message_id": "m",
                                  "chat_id": "oc_1", "request_id": "req-1"}
    return adapter


@pytest.mark.asyncio
async def test_feishu_click_from_someone_else_is_refused_and_keeps_state():
    entry = _pending("feishu-sess", requester="uid-a")
    adapter = _feishu_adapter()

    await adapter._resolve_approval(7, "once", "B", open_id="ou_b", user_id="uid-b", chat_id="oc_1")

    assert not entry.event.is_set()
    assert 7 in adapter._approval_state


@pytest.mark.asyncio
async def test_feishu_requester_click_resolves():
    entry = _pending("feishu-sess", requester="uid-a")
    adapter = _feishu_adapter()

    await adapter._resolve_approval(7, "deny", "A", open_id="ou_a", user_id="uid-a", chat_id="oc_1")

    assert entry.event.is_set() and entry.result == "deny"
    assert 7 not in adapter._approval_state


@pytest.mark.asyncio
async def test_feishu_falls_back_to_open_id_like_the_sender_profile():
    # Without the user_id scope the requester is recorded by open_id.
    entry = _pending("feishu-sess", requester="ou_a")
    adapter = _feishu_adapter()

    await adapter._resolve_approval(7, "once", "A", open_id="ou_a", chat_id="oc_1")

    assert entry.event.is_set()


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------

def _matrix_adapter(monkeypatch):
    monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@a:hs,@b:hs")
    from gateway.platforms.matrix import MatrixAdapter, _MatrixApprovalPrompt
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok",
                                           extra={"homeserver": "https://matrix.example.org"}))
    adapter._user_id = "@bot:hs"
    adapter._redact_bot_approval_reactions = AsyncMock()
    adapter._approval_prompts_by_event["$p"] = _MatrixApprovalPrompt(
        session_key="matrix-sess", chat_id="!r:hs", message_id="$p", request_id="req-1")
    adapter._approval_prompt_by_session["matrix-sess"] = "$p"
    return adapter


def _matrix_reaction(sender):
    return types.SimpleNamespace(sender=sender, event_id=f"$react-{sender}", room_id="!r:hs",
                                 content={"m.relates_to": {"event_id": "$p", "key": "✅"}})


@pytest.mark.asyncio
async def test_matrix_reaction_is_pinned_to_its_prompt(monkeypatch):
    entry = _pending("matrix-sess", requester="@a:hs", request_id="req-other")
    adapter = _matrix_adapter(monkeypatch)

    await adapter._on_reaction(_matrix_reaction("@a:hs"))

    assert not entry.event.is_set()


@pytest.mark.asyncio
async def test_matrix_reaction_from_requester_resolves(monkeypatch):
    entry = _pending("matrix-sess", requester="@a:hs")
    adapter = _matrix_adapter(monkeypatch)

    await adapter._on_reaction(_matrix_reaction("@b:hs"))
    assert not entry.event.is_set()

    await adapter._on_reaction(_matrix_reaction("@a:hs"))
    assert entry.event.is_set() and entry.result == "once"


# ---------------------------------------------------------------------------
# Invariant: every multi-user caller says who answered
# ---------------------------------------------------------------------------

# Callers that cannot know who answered, each with the reason.
_NO_APPROVER_ALLOWED = {
    # One user per TUI process.
    "tui_gateway/server.py",
    # A single shared bearer key: there is no per-user principal to bind to.
    "gateway/platforms/api_server.py",
    # operator_openid is not the id recorded as requester (see adapter comment).
    "gateway/platforms/qqbot/adapter.py",
}


def _resolve_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "resolve_gateway_approval":
                yield node


def test_every_multi_user_caller_passes_the_approver():
    offenders = []
    for path in sorted(_REPO.glob("gateway/**/*.py")) + sorted(_REPO.glob("plugins/platforms/**/*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel in _NO_APPROVER_ALLOWED:
            continue
        for call in _resolve_calls(path):
            if "approver" not in {kw.arg for kw in call.keywords}:
                offenders.append(f"{rel}:{call.lineno}")
    assert offenders == []
