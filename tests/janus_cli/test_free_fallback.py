"""Curated free-tier fallback presets and the training guard around them."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from janus_cli import free_fallback as ff


# ── Preset table ──────────────────────────────────────────────────────────


class TestPresets:
    def test_keys_are_unique(self):
        keys = [p.key for p in ff.FREE_FALLBACK_PRESETS]
        assert len(keys) == len(set(keys))

    def test_every_preset_states_its_data_use(self):
        for p in ff.FREE_FALLBACK_PRESETS:
            assert p.trains in ff.TRAINS_VALUES, p.key
            assert p.data_note.strip(), p.key
            assert p.signup_url.startswith("https://"), p.key
            assert p.limits.strip(), p.key

    def test_native_presets_use_a_real_provider_and_its_own_key(self):
        from janus_cli.models import CANONICAL_PROVIDERS
        from providers import get_provider_profile

        known = {p.slug for p in CANONICAL_PROVIDERS}
        for p in ff.FREE_FALLBACK_PRESETS:
            if p.provider == "custom":
                assert p.base_url.startswith("https://"), p.key
                continue
            assert p.provider in known, p.key
            assert p.key_env in get_provider_profile(p.provider).env_vars, p.key

    def test_every_key_env_is_a_registered_secret(self):
        from janus_cli.config import OPTIONAL_ENV_VARS

        for p in ff.FREE_FALLBACK_PRESETS:
            assert p.key_env in OPTIONAL_ENV_VARS, p.key
            assert OPTIONAL_ENV_VARS[p.key_env].get("password") is True, p.key

    def test_custom_preset_entry_carries_its_endpoint_and_key_env(self):
        custom = next(p for p in ff.FREE_FALLBACK_PRESETS if p.provider == "custom")

        entry = ff.preset_entry(custom)

        assert entry["base_url"] == custom.base_url
        assert entry["key_env"] == custom.key_env
        assert entry["free_preset"] == custom.key


def _preset(trains):
    return next(p for p in ff.FREE_FALLBACK_PRESETS if p.trains == trains)


# ── Guard ─────────────────────────────────────────────────────────────────


class TestGuard:
    def test_training_free_tier_without_consent_is_blocked(self):
        entry = ff.preset_entry(_preset("yes"))

        assert ff.blocked_by_training_guard(entry, allow_training_providers=False)

    def test_consent_on_the_entry_unblocks_it(self):
        entry = ff.preset_entry(_preset("yes"), allow_training=True)

        assert not ff.blocked_by_training_guard(entry, allow_training_providers=False)

    def test_global_allow_unblocks_it(self):
        entry = ff.preset_entry(_preset("yes"))

        assert not ff.blocked_by_training_guard(entry, allow_training_providers=True)

    def test_free_tier_that_does_not_train_is_never_blocked(self):
        entry = ff.preset_entry(_preset("no"))

        assert not ff.blocked_by_training_guard(entry, allow_training_providers=False)

    def test_entries_the_user_added_themselves_are_untouched(self):
        assert not ff.blocked_by_training_guard(
            {"provider": "gemini", "model": "gemini-flash-latest"}, allow_training_providers=False
        )

    def test_unknown_preset_counts_as_may_train(self):
        entry = {"provider": "custom", "model": "m", "free_preset": "gone-provider"}

        assert ff.blocked_by_training_guard(entry, allow_training_providers=False)

    def test_classification_comes_from_the_table_not_the_saved_entry(self):
        # A stale "trains: no" copied into config must not override the table.
        entry = {**ff.preset_entry(_preset("yes")), "trains": "no"}

        assert ff.blocked_by_training_guard(entry, allow_training_providers=False)


# ── Runtime (agent fallback activation) ───────────────────────────────────


def _make_agent(fallback_model):
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
    agent.client = MagicMock()
    agent._emit_warning = MagicMock()
    return agent


def _client():
    c = MagicMock()
    c.base_url = "https://fallback.example/v1"
    c.api_key = "fb-key"
    return c


class TestRuntime:
    def test_unconsented_training_free_tier_is_skipped_for_the_next_entry(self):
        free = ff.preset_entry(_preset("yes"))
        agent = _make_agent([free, {"provider": "openai", "model": "gpt-4o"}])

        with patch("agent.auxiliary_client.resolve_provider_client",
                   return_value=(_client(), "gpt-4o")) as rpc:
            assert agent._try_activate_fallback() is True

        assert agent.model == "gpt-4o"
        assert [c.args[0] for c in rpc.call_args_list] == ["openai"]

    def test_switch_to_a_free_tier_warns_right_away(self):
        preset = _preset("yes")
        agent = _make_agent([ff.preset_entry(preset, allow_training=True)])

        with patch("agent.auxiliary_client.resolve_provider_client",
                   return_value=(_client(), preset.model)):
            assert agent._try_activate_fallback() is True

        agent._emit_warning.assert_called_once()
        assert preset.data_note in agent._emit_warning.call_args.args[0]

    def test_switch_to_a_paid_entry_does_not_warn(self):
        agent = _make_agent([{"provider": "openai", "model": "gpt-4o"}])

        with patch("agent.auxiliary_client.resolve_provider_client",
                   return_value=(_client(), "gpt-4o")):
            assert agent._try_activate_fallback() is True

        agent._emit_warning.assert_not_called()

    def test_global_allow_in_config_lets_the_entry_run(self):
        from janus_constants import get_janus_home

        (get_janus_home() / "config.yaml").write_text(
            yaml.safe_dump({"fallback": {"allow_training_providers": True}}), encoding="utf-8"
        )
        preset = _preset("yes")
        agent = _make_agent([ff.preset_entry(preset)])

        with patch("agent.auxiliary_client.resolve_provider_client",
                   return_value=(_client(), preset.model)):
            assert agent._try_activate_fallback() is True


# ── `janus fallback add --free` ───────────────────────────────────────────


@pytest.fixture
def wizard(monkeypatch):
    from janus_constants import get_janus_home

    cfg = get_janus_home() / "config.yaml"
    cfg.write_text(yaml.safe_dump({"model": {"default": "claude", "provider": "anthropic"}}),
                   encoding="utf-8")
    env = {}
    answers = []
    secrets = []
    monkeypatch.setattr("janus_cli.config.get_env_value", lambda k: env.get(k))
    monkeypatch.setattr("janus_cli.config.save_env_value", lambda k, v: env.__setitem__(k, v))
    monkeypatch.setattr("builtins.input", lambda *a, **k: answers.pop(0) if answers else "")
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: secrets.pop(0) if secrets else "")
    monkeypatch.setattr("janus_cli.main._require_tty", lambda name: None)

    def chain():
        return yaml.safe_load(cfg.read_text(encoding="utf-8")).get("fallback_providers") or []

    return SimpleNamespace(env=env, answers=answers, secrets=secrets, chain=chain)


def _pick(preset):
    return str(ff.FREE_FALLBACK_PRESETS.index(preset) + 1)


def _add_free():
    from janus_cli.fallback_cmd import cmd_fallback

    cmd_fallback(SimpleNamespace(fallback_command="add", free=True))


class TestAddFree:
    def test_no_training_preset_is_added_after_saving_the_key(self, wizard):
        preset = _preset("no")
        wizard.answers.extend([_pick(preset), ""])     # pick, keep default model
        wizard.secrets.append("key-123")

        _add_free()

        assert wizard.env[preset.key_env] == "key-123"
        (entry,) = wizard.chain()
        assert entry["free_preset"] == preset.key and entry["model"] == preset.model
        assert "allow_training" not in entry

    def test_training_preset_needs_an_explicit_yes(self, wizard):
        preset = _preset("yes")
        wizard.answers.extend([_pick(preset), "", "n"])
        wizard.secrets.append("key-123")

        _add_free()

        assert wizard.chain() == []

    def test_yes_records_consent_on_the_entry(self, wizard):
        preset = _preset("yes")
        wizard.answers.extend([_pick(preset), "", "y"])
        wizard.secrets.append("key-123")

        _add_free()

        (entry,) = wizard.chain()
        assert entry["allow_training"] is True

    def test_model_can_be_overridden(self, wizard):
        preset = _preset("no")
        wizard.answers.extend([_pick(preset), "some/other-model"])
        wizard.secrets.append("key-123")

        _add_free()

        assert wizard.chain()[0]["model"] == "some/other-model"

    def test_existing_key_is_not_asked_again(self, wizard):
        preset = _preset("no")
        wizard.env[preset.key_env] = "already-there"
        wizard.answers.extend([_pick(preset), ""])
        wizard.secrets.append("SHOULD-NOT-BE-USED")

        _add_free()

        assert wizard.env[preset.key_env] == "already-there"
        assert len(wizard.chain()) == 1

    def test_blank_key_adds_nothing(self, wizard):
        wizard.answers.extend([_pick(_preset("no")), ""])

        _add_free()

        assert wizard.chain() == []

    def test_cancelled_pick_adds_nothing(self, wizard):
        _add_free()

        assert wizard.chain() == []

    def test_same_preset_twice_is_skipped(self, wizard):
        preset = _preset("no")
        wizard.env[preset.key_env] = "k"
        wizard.answers.extend([_pick(preset), "", _pick(preset), ""])

        _add_free()
        _add_free()

        assert len(wizard.chain()) == 1

    def test_parser_accepts_the_flag(self, monkeypatch):
        import sys

        seen = {}
        monkeypatch.setattr("janus_cli.fallback_cmd.cmd_fallback",
                            lambda args: seen.update(free=getattr(args, "free", None)))
        monkeypatch.setattr(sys, "argv", ["janus", "fallback", "add", "--free"])
        from janus_cli.main import main

        main()

        assert seen["free"] is True
