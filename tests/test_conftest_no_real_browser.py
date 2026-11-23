"""Tests must never open a real browser window.

OAuth login tests that forgot to stub ``webbrowser.open`` opened the real xAI
consent page in the developer's default browser on every local run (CI has no
display, so it never noticed). ``tests/conftest.py`` replaces the launcher for
every test; this checks that it does.
"""

from __future__ import annotations

import webbrowser

import pytest


@pytest.mark.parametrize("launcher", ["open", "open_new", "open_new_tab"])
def test_webbrowser_launchers_never_reach_a_real_browser(launcher, monkeypatch):
    # The stdlib launchers resolve a controller through webbrowser.get();
    # reaching it means a real browser would have been started.
    def _real_lookup(*_a, **_k):
        pytest.fail(f"webbrowser.{launcher}() tried to start a real browser")

    monkeypatch.setattr(webbrowser, "get", _real_lookup)

    opened = getattr(webbrowser, launcher)("https://example.invalid/oauth")

    # Report "could not open", like a headless CI box, so callers take
    # their print-the-URL fallback.
    assert opened is False
