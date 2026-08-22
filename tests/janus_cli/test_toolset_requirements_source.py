"""Toolset requirements must come from the LIVE registry, not an empty snapshot.

``model_tools.TOOLSET_REQUIREMENTS`` became a permanently-empty deprecated dict
when 70dbbfa moved tool discovery into the entry points. banner.py and doctor.py
still read it.

For the banner that is a visible misreport rather than a cosmetic one: an
unavailable toolset carrying a ``check_fn`` is *lazy* (honcho, homeassistant —
the check simply hasn't run yet) and renders yellow, while one without is
genuinely misconfigured and renders red. With the lookup always returning ``{}``
no toolset ever had a ``check_fn``, so every lazy toolset was painted red — the
exact confusion the code comments say the distinction exists to prevent.
"""
from __future__ import annotations

import pytest

from janus_cli import banner


@pytest.fixture(scope="module")
def live_requirements() -> dict:
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    return registry.get_toolset_requirements()


class TestToolsetRequirementsSource:
    def test_live_source_is_not_empty(self, live_requirements):
        assert live_requirements, "registry reported no toolset requirements at all"

    def test_banner_resolves_the_same_requirements(self, live_requirements):
        assert set(banner._toolset_requirements()) == set(live_requirements)


class TestUnavailableToolClassification:
    def test_toolset_with_check_fn_is_lazy_not_disabled(self, monkeypatch):
        monkeypatch.setattr(
            banner, "_toolset_requirements",
            lambda: {"honcho": {"name": "honcho", "check_fn": lambda: True}},
        )
        disabled, lazy = banner._classify_unavailable_tools(
            [{"name": "honcho", "tools": ["honcho_search", "honcho_add"]}]
        )
        assert lazy == {"honcho_search", "honcho_add"}
        assert disabled == set()

    def test_toolset_without_check_fn_is_disabled(self, monkeypatch):
        monkeypatch.setattr(
            banner, "_toolset_requirements",
            lambda: {"web": {"name": "web"}},
        )
        disabled, lazy = banner._classify_unavailable_tools(
            [{"name": "web", "tools": ["web_search"]}]
        )
        assert disabled == {"web_search"}
        assert lazy == set()

    def test_unknown_toolset_is_disabled(self, monkeypatch):
        """No requirement entry means we cannot claim it is merely lazy."""
        monkeypatch.setattr(banner, "_toolset_requirements", lambda: {})
        disabled, lazy = banner._classify_unavailable_tools(
            [{"name": "mystery", "tools": ["mystery_tool"]}]
        )
        assert disabled == {"mystery_tool"}
        assert lazy == set()

    def test_mixed_toolsets_are_split(self, monkeypatch):
        monkeypatch.setattr(
            banner, "_toolset_requirements",
            lambda: {
                "honcho": {"check_fn": lambda: True},
                "web": {},
            },
        )
        disabled, lazy = banner._classify_unavailable_tools([
            {"name": "honcho", "tools": ["honcho_search"]},
            {"name": "web", "tools": ["web_search"]},
        ])
        assert lazy == {"honcho_search"}
        assert disabled == {"web_search"}

    def test_empty_input_yields_empty_sets(self, monkeypatch):
        monkeypatch.setattr(banner, "_toolset_requirements", lambda: {})
        assert banner._classify_unavailable_tools([]) == (set(), set())
