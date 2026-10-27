"""Proactive first-run onboarding.

On a fresh install, a new user used to discover ``janus setup`` only through a
downstream error string ("Set OPENROUTER_API_KEY or run: janus setup"). This
module detects the cold-start case at CLI entry and offers the existing wizard
once, interactively — the "frictionless first five minutes" story.

Gating is deliberately strict so the prompt can only ever appear where a human
is actually watching a fresh, unconfigured install:

  * no ``config.yaml`` under ``get_janus_home()`` and no provider configured
  * stdin AND stdout are TTYs
  * not a managed install (Nix / Homebrew can't write config)
  * no headless marker (gateway, cron, kanban) in the environment
  * not a scripted query (``janus "prompt"`` / ``--image``)
  * not already offered (a stamp file latches it to exactly one ask, ever)

``JANUS_SKIP_FIRST_RUN=1`` is the escape hatch. ``-z/--oneshot`` never reaches
this code path at all.

Prompt-cache safety: this runs before any ``JanusCLI``/``AIAgent`` or system
prompt exists, so nothing mid-conversation is ever touched. Best-effort —
every failure path degrades to "don't prompt".
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Any of these present (non-empty) in the environment means a headless surface
# owns this process — never prompt. Names mirror tools/approval.py, cli.py and
# janus_cli/kanban_db.py.
_HEADLESS_MARKERS = (
    "JANUS_CRON_SESSION",
    "JANUS_GATEWAY_SESSION",
    "_JANUS_GATEWAY",
    "JANUS_KANBAN_TASK",
)


def _stamp_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / ".first_run_offered"


def _already_offered() -> bool:
    try:
        return _stamp_path().exists()
    except Exception:
        return True  # can't tell → never nag


def _mark_offered() -> None:
    try:
        p = _stamp_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("1\n", encoding="utf-8")
    except Exception as exc:
        logger.debug("could not write first-run stamp: %s", exc)


def _is_tty() -> bool:
    try:
        from janus_cli.setup import is_interactive_stdin
        return bool(is_interactive_stdin() and sys.stdout.isatty())
    except Exception:
        return False


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _provider_configured() -> bool:
    """Any usable inference provider already set up (env, .env, OAuth, config)."""
    try:
        from janus_cli.main import _has_any_provider_configured
        return bool(_has_any_provider_configured())
    except Exception:
        return True  # unknown → assume configured → don't prompt


def should_offer_first_run(args: Any = None) -> bool:
    """True only for a fresh, unconfigured, interactive, human-driven launch."""
    try:
        if _env_truthy("JANUS_SKIP_FIRST_RUN"):
            return False
        if os.environ.get("JANUS_IGNORE_USER_CONFIG") == "1":
            return False
        if any(os.environ.get(m) for m in _HEADLESS_MARKERS):
            return False
        if getattr(args, "query", None) or getattr(args, "image", None):
            return False
        if not _is_tty():
            return False
        from janus_cli.config import get_config_path, is_managed
        if is_managed():
            return False
        if get_config_path().exists():
            return False
        if _already_offered():
            return False
        if _provider_configured():
            return False
        return True
    except Exception:
        return False


def _print_skip_hints() -> None:
    print("  Skipped. Configure anytime with:        janus setup")
    print("  Fastest start (free OAuth, no API keys): janus setup --portal")
    print("  Or export OPENROUTER_API_KEY / OPENAI_API_KEY and run janus again.")
    print()


def maybe_offer_first_run(
    args: Any = None, *, input_fn: Callable[[str], str] = input
) -> Optional[bool]:
    """Offer the setup wizard once on a cold start.

    Returns True if the wizard ran, False if the user declined (or interrupted),
    None if the offer was skipped (gated out). Stamps BEFORE asking so the
    prompt appears exactly once regardless of the answer.
    """
    if not should_offer_first_run(args):
        return None
    _mark_offered()
    print()
    print("  ⚕ Welcome to Janus — no configuration found yet.")
    print("    The setup wizard configures your model/provider, where commands run,")
    print("    messaging, and the read-only learning loop. About a minute.")
    try:
        reply = str(input_fn("  Run the setup wizard now? [Y/n] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        _print_skip_hints()
        return False
    if reply and reply not in {"y", "yes"}:
        _print_skip_hints()
        return False
    try:
        from argparse import Namespace
        from janus_cli.setup import run_setup_wizard
        run_setup_wizard(Namespace(
            section=None, non_interactive=False, reset=False,
            reconfigure=False, quick=False, portal=False,
        ))
        return True
    except (EOFError, KeyboardInterrupt):
        print()
        _print_skip_hints()
        return False
    except Exception as exc:
        logger.debug("first-run wizard failed: %s", exc)
        _print_skip_hints()
        return False
