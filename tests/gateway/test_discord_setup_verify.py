"""Discord setup verifies the bot token and the privileged intents Janus needs."""

import httpx
import pytest

from plugins.platforms.discord import adapter as discord_adapter

CONTENT = 1 << 18
CONTENT_LIMITED = 1 << 19
MEMBERS = 1 << 14


def _transport(status=200, flags=0, name="Janus Bot", seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"message": "401: Unauthorized", "code": 0})
        return httpx.Response(
            200,
            json={"id": "42", "name": "Janus App", "flags": flags, "bot": {"username": name}},
        )

    return httpx.MockTransport(handler)


class TestVerifyDiscordToken:
    def test_valid_token_reports_bot_and_intents(self):
        seen = []
        result = discord_adapter.verify_discord_token(
            "tok", transport=_transport(flags=CONTENT | MEMBERS, seen=seen)
        )

        assert result["ok"] is True
        assert result["name"] == "Janus Bot"
        assert result["message_content"] is True
        assert result["members"] is True
        assert seen[0].headers["Authorization"] == "Bot tok"
        assert seen[0].url.path.endswith("/applications/@me")

    def test_limited_flag_counts_as_enabled(self):
        # Unverified bots (<100 servers) get the *_LIMITED flag instead.
        result = discord_adapter.verify_discord_token("tok", transport=_transport(flags=CONTENT_LIMITED))

        assert result["message_content"] is True
        assert result["members"] is False

    def test_missing_message_content_intent_is_reported(self):
        result = discord_adapter.verify_discord_token("tok", transport=_transport(flags=0))

        assert result["ok"] is True
        assert result["message_content"] is False

    def test_rejected_token_is_invalid(self):
        result = discord_adapter.verify_discord_token("bad", transport=_transport(status=401))

        assert result["ok"] is False
        assert result["invalid"] is True

    def test_network_failure_is_not_treated_as_invalid(self):
        def boom(request):
            raise httpx.ConnectError("offline", request=request)

        result = discord_adapter.verify_discord_token("tok", transport=httpx.MockTransport(boom))

        assert result["ok"] is False
        assert result["invalid"] is False


class TestDiscordIntentProblems:
    def test_missing_content_intent_is_always_a_problem(self):
        problems = discord_adapter.discord_intent_problems(
            {"ok": True, "message_content": False, "members": True}, needs_members=False
        )

        assert any("Message Content" in p for p in problems)

    def test_members_intent_only_matters_when_needed(self):
        info = {"ok": True, "message_content": True, "members": False}

        assert discord_adapter.discord_intent_problems(info, needs_members=False) == []
        assert any(
            "Server Members" in p
            for p in discord_adapter.discord_intent_problems(info, needs_members=True)
        )

    @pytest.mark.parametrize(
        "users, roles, expected",
        [("123,456", "", False), ("123,alice", "", True), ("", "789", True), ("", "", False)],
    )
    def test_needs_members_intent_mirrors_the_adapter(self, users, roles, expected):
        assert discord_adapter.discord_needs_members_intent(users, roles) is expected


class TestInteractiveSetup:
    @pytest.fixture
    def wizard(self, monkeypatch):
        env = {}
        answers = []
        out = []
        import janus_cli.cli_output as cli_output
        import janus_cli.config as cli_config

        monkeypatch.setattr(cli_config, "get_env_value", lambda k: env.get(k))
        monkeypatch.setattr(cli_config, "save_env_value", lambda k, v: env.__setitem__(k, v))
        monkeypatch.setattr(cli_output, "prompt", lambda *a, **k: answers.pop(0) if answers else "")
        monkeypatch.setattr(cli_output, "prompt_yes_no", lambda *a, **k: False)
        for name in ("print_info", "print_success", "print_warning", "print_error", "print_header"):
            monkeypatch.setattr(cli_output, name, lambda text, _n=name: out.append((_n, text)))
        return env, answers, out

    def test_rejected_token_is_not_saved_and_reprompts(self, wizard, monkeypatch):
        env, answers, out = wizard
        tokens = []

        def fake_verify(token, **_kw):
            tokens.append(token)
            if token == "good":
                return {"ok": True, "name": "Bot", "message_content": True, "members": False}
            return {"ok": False, "invalid": True, "error": "401"}

        monkeypatch.setattr(discord_adapter, "verify_discord_token", fake_verify)
        answers.extend(["bad", "good", "123", ""])

        discord_adapter.interactive_setup()

        assert tokens == ["bad", "good"]
        assert env["DISCORD_BOT_TOKEN"] == "good"

    def test_unreachable_api_still_saves_the_token(self, wizard, monkeypatch):
        env, answers, out = wizard
        monkeypatch.setattr(
            discord_adapter,
            "verify_discord_token",
            lambda token, **_kw: {"ok": False, "invalid": False, "error": "offline"},
        )
        answers.extend(["tok", "123", ""])

        discord_adapter.interactive_setup()

        assert env["DISCORD_BOT_TOKEN"] == "tok"
        assert any(kind == "print_warning" and "verify" in text.lower() for kind, text in out)

    def test_missing_content_intent_warns_with_the_portal_fix(self, wizard, monkeypatch):
        env, answers, out = wizard
        monkeypatch.setattr(
            discord_adapter,
            "verify_discord_token",
            lambda token, **_kw: {"ok": True, "id": "42", "name": "Bot", "message_content": False, "members": False},
        )
        answers.extend(["tok", "123", ""])

        discord_adapter.interactive_setup()

        warnings = " ".join(text for kind, text in out if kind == "print_warning")
        assert "Message Content" in warnings
        assert "Privileged Gateway Intents" in " ".join(text for _k, text in out)

    def test_kept_username_allowlist_still_needs_the_members_intent(self, wizard, monkeypatch):
        env, answers, out = wizard
        env["DISCORD_ALLOWED_USERS"] = "alice"  # saved earlier; prompt left empty keeps it
        monkeypatch.setattr(
            discord_adapter,
            "verify_discord_token",
            lambda token, **_kw: {"ok": True, "id": "42", "name": "Bot", "message_content": True, "members": False},
        )
        answers.extend(["tok", "", ""])

        discord_adapter.interactive_setup()

        warnings = " ".join(text for kind, text in out if kind == "print_warning")
        assert "Server Members" in warnings
