"""batch_runner must resolve tool names from the LIVE registry.

Commit 70dbbfa moved tool discovery out of ``import model_tools`` and into each
entry point, leaving ``model_tools.TOOL_TO_TOOLSET_MAP`` a permanently-empty
deprecated snapshot. cli.py, janus_cli/main.py and gateway/runner.py were all
updated to call ``discover_builtin_tools()``; batch_runner was not, and it kept
deriving its tool set from that empty dict at import time.

Two silent consequences, both pinned here:

* ``_normalize_tool_stats`` / ``_normalize_tool_error_counts`` zero-fill every
  known tool so the Arrow/Parquet schema stays consistent across shards for
  HuggingFace datasets. With an empty set the loops did nothing.
* Trajectory combination filters entries whose tool_stats contain an unknown
  tool. With an empty set EVERY tool was unknown, so every entry that used a
  tool was discarded as "corrupted" and never written to the combined output.
"""
from __future__ import annotations

import pytest

import batch_runner


@pytest.fixture(scope="module")
def registered_tools() -> set[str]:
    """The tool names the live registry knows about, discovery included."""
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    return set(registry.get_tool_to_toolset_map())


class TestAllPossibleTools:
    def test_is_not_empty(self, registered_tools):
        """The empty set is the bug — it silently disabled two features."""
        assert batch_runner._all_possible_tools(), (
            "batch_runner resolved zero tools; the Arrow zero-fill and the "
            "trajectory-corruption filter both degrade silently when this is empty"
        )

    def test_matches_the_live_registry(self, registered_tools):
        assert set(batch_runner._all_possible_tools()) == registered_tools

    def test_contains_core_builtins(self, registered_tools):
        # Relationship, not a snapshot: whatever the registry holds must be
        # what batch_runner sees, and a few load-bearing tools must be in it.
        for name in ("read_file", "write_file", "terminal"):
            if name in registered_tools:
                assert name in batch_runner._all_possible_tools()


class TestNormalizeToolStats:
    def test_zero_fills_every_registered_tool(self, registered_tools):
        normalized = batch_runner._normalize_tool_stats({})
        assert set(normalized) >= registered_tools
        for name in registered_tools:
            assert normalized[name] == {"count": 0, "success": 0, "failure": 0}

    def test_preserves_supplied_stats(self, registered_tools):
        name = sorted(registered_tools)[0]
        stats = {name: {"count": 3, "success": 2, "failure": 1}}
        normalized = batch_runner._normalize_tool_stats(stats)
        assert normalized[name] == {"count": 3, "success": 2, "failure": 1}
        assert set(normalized) >= registered_tools

    def test_does_not_alias_the_caller_dict(self, registered_tools):
        name = sorted(registered_tools)[0]
        stats = {name: {"count": 1, "success": 1, "failure": 0}}
        normalized = batch_runner._normalize_tool_stats(stats)
        normalized[name]["count"] = 999
        assert stats[name]["count"] == 1

    def test_keeps_unknown_tools(self):
        """A plugin tool absent from the registry must survive, not vanish."""
        normalized = batch_runner._normalize_tool_stats(
            {"some_plugin_tool": {"count": 1, "success": 1, "failure": 0}}
        )
        assert normalized["some_plugin_tool"] == {"count": 1, "success": 1, "failure": 0}


class TestNormalizeToolErrorCounts:
    def test_zero_fills_every_registered_tool(self, registered_tools):
        normalized = batch_runner._normalize_tool_error_counts({})
        assert set(normalized) >= registered_tools
        assert all(normalized[name] == 0 for name in registered_tools)

    def test_preserves_supplied_counts_and_unknown_tools(self, registered_tools):
        name = sorted(registered_tools)[0]
        normalized = batch_runner._normalize_tool_error_counts(
            {name: 4, "some_plugin_tool": 2}
        )
        assert normalized[name] == 4
        assert normalized["some_plugin_tool"] == 2


class TestInvalidToolDetection:
    """The trajectory-combination filter that was discarding real data."""

    def test_real_tool_names_are_not_flagged(self, registered_tools):
        stats = {name: {"count": 1} for name in sorted(registered_tools)[:5]}
        assert batch_runner._invalid_tool_names(stats) == []

    def test_bogus_tool_name_is_flagged(self):
        assert batch_runner._invalid_tool_names(
            {"definitely_not_a_real_tool": {"count": 1}}
        ) == ["definitely_not_a_real_tool"]

    def test_empty_stats_are_not_flagged(self):
        assert batch_runner._invalid_tool_names({}) == []

    def test_flags_only_the_unknown_names(self, registered_tools):
        known = sorted(registered_tools)[0]
        stats = {known: {"count": 1}, "bogus_tool": {"count": 1}}
        assert batch_runner._invalid_tool_names(stats) == ["bogus_tool"]
