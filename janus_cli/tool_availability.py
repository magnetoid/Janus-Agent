"""Direct-credential tool availability probes for setup/status surfaces.

Answers "would this tool backend actually run right now?" from config.yaml,
``.env`` keys and local binaries, using the same precedence the runtime uses.
Used by the setup wizard's summary; no network calls.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Tuple

from janus_cli.config import get_env_value
from tools.tool_backend_helpers import (
    fal_key_is_configured,
    has_direct_modal_credentials,
    normalize_browser_cloud_provider,
)


def _section(config: Dict[str, object], key: str) -> Dict[str, object]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def has_agent_browser() -> bool:
    """Return True when the ``agent-browser`` CLI is installed."""
    agent_browser_bin = shutil.which("agent-browser")
    local_bin = Path(__file__).parent.parent / "node_modules" / ".bin" / "agent-browser"
    return bool(agent_browser_bin or local_bin.exists())


def local_browser_runnable() -> bool:
    """Return True when the *local* browser backend would actually start.

    The ``agent-browser`` CLI being present is necessary but not sufficient for
    local mode: agent-browser also needs a Chromium build on disk (without one
    it hangs on first use until the command timeout fires), unless the
    Lightpanda engine is selected — text-only navigation needs no Chromium.

    This mirrors the local-mode tail of
    :func:`tools.browser_tool.check_browser_requirements`. Cloud providers
    (Browserbase, Browser Use, Firecrawl) host their own Chromium and therefore
    gate on :func:`has_agent_browser` alone.
    """
    if not has_agent_browser():
        return False
    try:
        from tools.browser_tool import _chromium_installed, _using_lightpanda_engine
    except Exception:
        # If the runtime probe can't be imported, fall back to binary presence
        # rather than crashing the setup/status surface.
        return True
    if _using_lightpanda_engine():
        return True
    return _chromium_installed()


_BROWSER_LABELS = {
    "browserbase": "Browserbase",
    "browser-use": "Browser Use",
    "firecrawl": "Firecrawl",
    "camofox": "Camofox",
    "local": "Local browser",
}


def browser_label(provider: str) -> str:
    return _BROWSER_LABELS.get(provider or "local", provider or "Local browser")


def browser_state(config: Dict[str, object]) -> Tuple[str, bool]:
    """Return ``(provider_label, available)`` for browser automation.

    Precedence matches the runtime: ``CAMOFOX_URL`` wins, then an explicit
    ``browser.cloud_provider``, then whichever cloud credentials are present,
    then the local browser.
    """
    browser_cfg = _section(config, "browser")
    explicit = "cloud_provider" in browser_cfg
    provider = normalize_browser_cloud_provider(
        browser_cfg.get("cloud_provider") if explicit else None
    )
    direct_camofox = bool(get_env_value("CAMOFOX_URL"))
    direct_browserbase = bool(
        get_env_value("BROWSERBASE_API_KEY") and get_env_value("BROWSERBASE_PROJECT_ID")
    )
    direct_browser_use = bool(get_env_value("BROWSER_USE_API_KEY"))
    direct_firecrawl = bool(
        get_env_value("FIRECRAWL_API_KEY") or get_env_value("FIRECRAWL_API_URL")
    )

    if direct_camofox:
        return browser_label("camofox"), True

    cli_present = has_agent_browser()
    if explicit:
        current = provider or "local"
        if current == "browserbase":
            return browser_label(current), bool(cli_present and direct_browserbase)
        if current == "browser-use":
            return browser_label(current), bool(cli_present and direct_browser_use)
        if current == "firecrawl":
            return browser_label(current), bool(cli_present and direct_firecrawl)
        if current == "camofox":
            return browser_label(current), False
        return browser_label("local"), local_browser_runnable()

    if direct_browser_use:
        return browser_label("browser-use"), cli_present
    if direct_browserbase:
        return browser_label("browserbase"), cli_present
    return browser_label("local"), local_browser_runnable()


_WEB_KEYS = (
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "PARALLEL_API_KEY",
    "TAVILY_API_KEY",
    "SEARXNG_URL",
)


def web_state(config: Dict[str, object]) -> Tuple[str, bool]:
    """Return ``(configured_backend, available)`` for web search/extract."""
    web_cfg = _section(config, "web")
    backend = str(web_cfg.get("backend") or "").strip().lower()
    search_backend = str(web_cfg.get("search_backend") or "").strip().lower()
    available = any(get_env_value(key) for key in _WEB_KEYS)
    return backend or search_backend, available


def image_gen_available() -> bool:
    """Return True when the built-in FAL image backend has a key."""
    return fal_key_is_configured()


def modal_direct_available() -> bool:
    """Return True when direct Modal credentials are configured."""
    return has_direct_modal_credentials()
