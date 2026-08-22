"""Tests for the setup wizard's returning-user behavior.

On an existing install:
- Bare `janus setup` drops straight into the full reconfigure wizard
  (every prompt shows the current value as its default).
- `janus setup --quick` runs the narrower "fill in missing items" flow.
- `janus setup --reconfigure` is a backwards-compat alias for the
  bare-setup default.

On a fresh install, all three are no-ops — fall through to first-time setup.
"""

from argparse import Namespace
from contextlib import ExitStack
from unittest.mock import patch

import pytest


def _make_setup_args(**overrides):
    return Namespace(
        non_interactive=overrides.get("non_interactive", False),
        section=overrides.get("section", None),
        reset=overrides.get("reset", False),
        reconfigure=overrides.get("reconfigure", False),
        quick=overrides.get("quick", False),
    )


@pytest.fixture
def existing_install(tmp_path, monkeypatch):
    """Simulate a returning user with an existing configured install."""
    home = tmp_path / ".janus"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("JANUS_HOME", str(home))
    return home


@pytest.fixture
def fresh_install(tmp_path, monkeypatch):
    """Simulate a first-time user with no existing configuration."""
    home = tmp_path / ".janus"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("JANUS_HOME", str(home))
    return home


def _enter_existing_install_patches(stack, **extra):
    """Apply standard existing-install mocks via an ExitStack.

    Returns a dict of mocks from the `extra` kwargs (which map mock-name to
    target path) so callers can assert on them.
    """
    # Unconditional mocks (no return values to assert against).
    for target, kwargs in [
        ("janus_cli.setup.ensure_janus_home", {}),
        ("janus_cli.setup.is_interactive_stdin", {"return_value": True}),
        ("janus_cli.config.is_managed", {"return_value": False}),
        ("janus_cli.setup.load_config", {"return_value": {}}),
        ("janus_cli.setup.save_config", {}),
        ("janus_cli.setup.get_env_value", {"return_value": None}),
        ("janus_cli.auth.get_active_provider", {"return_value": "openrouter"}),
        ("janus_cli.setup._print_setup_summary", {}),
        ("janus_cli.setup._offer_openclaw_migration", {"return_value": False}),
        # Wizard sections this suite does not assert on, stubbed so the
        # full-reconfigure run stays non-interactive.
        ("janus_cli.setup.setup_consensus", {}),
    ]:
        stack.enter_context(patch(target, **kwargs))

    # Any prompt the wizard reaches that isn't stubbed above would otherwise
    # block on stdin (pytest raises an opaque "reading from stdin while output
    # is captured" OSError). Fail with a message that names the culprit
    # instead, so a newly added wizard section is obvious.
    def _unstubbed_prompt(prompt=""):
        raise AssertionError(
            "setup wizard read stdin during a non-interactive test "
            f"(prompt: {prompt!r}) — a wizard section is missing a stub in "
            "_enter_existing_install_patches"
        )

    stack.enter_context(patch("builtins.input", _unstubbed_prompt))

    # Named mocks caller wants to assert on.
    named = {}
    for name, target in extra.items():
        named[name] = stack.enter_context(patch(target))
    return named


def _enter_fresh_install_patches(stack, **extra):
    for target, kwargs in [
        ("janus_cli.setup.ensure_janus_home", {}),
        ("janus_cli.setup.is_interactive_stdin", {"return_value": True}),
        ("janus_cli.config.is_managed", {"return_value": False}),
        ("janus_cli.setup.load_config", {"return_value": {}}),
        ("janus_cli.setup.save_config", {}),
        ("janus_cli.auth.get_active_provider", {"return_value": None}),
        ("janus_cli.setup.get_env_value", {"return_value": None}),
        ("janus_cli.setup._offer_openclaw_migration", {"return_value": False}),
    ]:
        stack.enter_context(patch(target, **kwargs))

    named = {}
    for name, target_spec in extra.items():
        if isinstance(target_spec, tuple):
            target, kwargs = target_spec
            named[name] = stack.enter_context(patch(target, **kwargs))
        else:
            named[name] = stack.enter_context(patch(target_spec))
    return named


class TestExistingInstallDefault:
    """Bare `janus setup` on an existing install = full reconfigure wizard."""

    def test_bare_setup_runs_full_reconfigure_without_menu(self, existing_install):
        """No menu, no prompt_choice — just run every section in sequence."""
        args = _make_setup_args()  # no flags

        with ExitStack() as stack:
            m = _enter_existing_install_patches(
                stack,
                prompt_choice="janus_cli.setup.prompt_choice",
                quick="janus_cli.setup._run_quick_setup",
                model="janus_cli.setup.setup_model_provider",
                terminal="janus_cli.setup.setup_terminal_backend",
                agent="janus_cli.setup.setup_agent_settings",
                gateway="janus_cli.setup.setup_gateway",
                tools="janus_cli.setup.setup_tools",
            )
            from janus_cli.setup import run_setup_wizard
            run_setup_wizard(args)

        # No menu shown.
        m["prompt_choice"].assert_not_called()
        # Quick-setup path NOT taken.
        m["quick"].assert_not_called()
        # Model/terminal/gateway/tools run; agent settings are no longer
        # prompted on existing installs (they keep their tuned values).
        m["model"].assert_called_once()
        m["terminal"].assert_called_once()
        m["agent"].assert_not_called()
        m["gateway"].assert_called_once()
        m["tools"].assert_called_once()

    def test_reconfigure_flag_is_backwards_compat_noop(self, existing_install):
        """`janus setup --reconfigure` behaves the same as bare `janus setup`."""
        args = _make_setup_args(reconfigure=True)

        with ExitStack() as stack:
            m = _enter_existing_install_patches(
                stack,
                prompt_choice="janus_cli.setup.prompt_choice",
                model="janus_cli.setup.setup_model_provider",
                terminal="janus_cli.setup.setup_terminal_backend",
                agent="janus_cli.setup.setup_agent_settings",
                gateway="janus_cli.setup.setup_gateway",
                tools="janus_cli.setup.setup_tools",
            )
            from janus_cli.setup import run_setup_wizard
            run_setup_wizard(args)

        m["prompt_choice"].assert_not_called()
        m["model"].assert_called_once()
        m["terminal"].assert_called_once()
        m["agent"].assert_not_called()
        m["gateway"].assert_called_once()
        m["tools"].assert_called_once()


class TestQuickFlag:
    """`--quick` on an existing install runs the fill-missing flow."""

    def test_quick_flag_runs_quick_setup_only(self, existing_install):
        args = _make_setup_args(quick=True)

        with ExitStack() as stack:
            m = _enter_existing_install_patches(
                stack,
                quick="janus_cli.setup._run_quick_setup",
                model="janus_cli.setup.setup_model_provider",
                terminal="janus_cli.setup.setup_terminal_backend",
                agent="janus_cli.setup.setup_agent_settings",
                gateway="janus_cli.setup.setup_gateway",
                tools="janus_cli.setup.setup_tools",
            )
            from janus_cli.setup import run_setup_wizard
            run_setup_wizard(args)

        m["quick"].assert_called_once()
        # Full reconfigure sections must NOT run.
        m["model"].assert_not_called()
        m["terminal"].assert_not_called()
        m["agent"].assert_not_called()
        m["gateway"].assert_not_called()
        m["tools"].assert_not_called()


class TestFreshInstall:
    """On a fresh install (no active provider), flags are no-ops."""

    def test_bare_setup_runs_first_time_flow(self, fresh_install):
        args = _make_setup_args()

        with ExitStack() as stack:
            m = _enter_fresh_install_patches(
                stack,
                prompt=("janus_cli.setup.prompt_choice", {"return_value": 0}),
                first="janus_cli.setup._run_first_time_quick_setup",
            )
            from janus_cli.setup import run_setup_wizard
            run_setup_wizard(args)

        m["prompt"].assert_called_once()  # quick-vs-full prompt
        m["first"].assert_called_once()

    def test_reconfigure_on_fresh_install_falls_through(self, fresh_install):
        args = _make_setup_args(reconfigure=True)

        with ExitStack() as stack:
            m = _enter_fresh_install_patches(
                stack,
                prompt=("janus_cli.setup.prompt_choice", {"return_value": 0}),
                first="janus_cli.setup._run_first_time_quick_setup",
            )
            from janus_cli.setup import run_setup_wizard
            run_setup_wizard(args)

        m["prompt"].assert_called_once()
        m["first"].assert_called_once()

    def test_quick_on_fresh_install_falls_through(self, fresh_install):
        args = _make_setup_args(quick=True)

        with ExitStack() as stack:
            m = _enter_fresh_install_patches(
                stack,
                prompt=("janus_cli.setup.prompt_choice", {"return_value": 0}),
                first="janus_cli.setup._run_first_time_quick_setup",
            )
            from janus_cli.setup import run_setup_wizard
            run_setup_wizard(args)

        m["prompt"].assert_called_once()
        m["first"].assert_called_once()


class TestArgparse:
    """The flags are plumbed through argparse to cmd_setup."""

    def test_reconfigure_flag_reaches_cmd_setup(self, monkeypatch):
        import sys
        from janus_cli.main import main

        captured = {}
        monkeypatch.setattr(
            "janus_cli.setup.run_setup_wizard",
            lambda args: captured.setdefault("args", args),
        )
        monkeypatch.setattr(sys, "argv", ["janus", "setup", "--reconfigure"])
        try:
            main()
        except SystemExit:
            pass
        assert captured["args"].reconfigure is True
        assert captured["args"].quick is False

    def test_quick_flag_reaches_cmd_setup(self, monkeypatch):
        import sys
        from janus_cli.main import main

        captured = {}
        monkeypatch.setattr(
            "janus_cli.setup.run_setup_wizard",
            lambda args: captured.setdefault("args", args),
        )
        monkeypatch.setattr(sys, "argv", ["janus", "setup", "--quick"])
        try:
            main()
        except SystemExit:
            pass
        assert captured["args"].quick is True
        assert captured["args"].reconfigure is False

    def test_bare_setup_has_both_flags_false(self, monkeypatch):
        import sys
        from janus_cli.main import main

        captured = {}
        monkeypatch.setattr(
            "janus_cli.setup.run_setup_wizard",
            lambda args: captured.setdefault("args", args),
        )
        monkeypatch.setattr(sys, "argv", ["janus", "setup"])
        try:
            main()
        except SystemExit:
            pass
        assert captured["args"].reconfigure is False
        assert captured["args"].quick is False
