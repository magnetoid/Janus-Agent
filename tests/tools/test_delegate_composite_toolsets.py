"""Tests for composite toolset expansion in delegate_task intersection.

These assert the *rule* ``_expand_parent_toolsets`` implements — a toolset name
is recognised for a parent exactly when the parent already holds every tool in
it — rather than a hand-listed set of names.  Which names satisfy that rule is
data that legitimately moves whenever a tool is added to or moved between
toolsets (e.g. ``pagemem_remember`` joining ``browser``); the rule itself must
not.
"""

import unittest

from toolsets import TOOLSETS
from tools.delegate_tool import _expand_parent_toolsets

COMPOSITE = "janus-cli"


def _tools_of(name: str) -> set:
    return set(TOOLSETS.get(name, {}).get("tools", []))


def _contained_in(parent_tools: set) -> set:
    """Toolset names whose tools the parent fully holds."""
    return {
        name
        for name, definition in TOOLSETS.items()
        if (tools := set(definition.get("tools", []))) and tools <= parent_tools
    }


class TestExpandParentToolsets(unittest.TestCase):
    """Verify _expand_parent_toolsets recognises individual toolsets within composites."""

    def test_composite_expands_to_the_toolsets_it_fully_contains(self):
        """Every toolset the composite fully contains is recognised by name.

        A child asking for a narrower toolset must not be rejected merely
        because the parent spells its grant as a composite.
        """
        parent_tools = _tools_of(COMPOSITE)
        self.assertTrue(parent_tools, f"{COMPOSITE} must define tools")

        expanded = _expand_parent_toolsets({COMPOSITE})
        contained = _contained_in(parent_tools)

        # Guard against a vacuous pass: the composite really does bundle
        # narrower toolsets, so there is something to expand.
        self.assertTrue(contained - {COMPOSITE})
        for name in contained:
            self.assertIn(
                name,
                expanded,
                f"{name}'s tools are all held by {COMPOSITE} but it did not expand",
            )

    def test_expansion_never_grants_a_tool_the_parent_lacks(self):
        """Soundness: expansion may only name toolsets the parent covers.

        This is the security half of the intersection — a subagent must not
        gain tools its parent does not have.
        """
        for parent_name, definition in TOOLSETS.items():
            parent_tools = set(definition.get("tools", []))
            if not parent_tools:
                continue
            expanded = _expand_parent_toolsets({parent_name})
            for name in expanded - {parent_name}:
                self.assertLessEqual(
                    _tools_of(name),
                    parent_tools,
                    f"expanding {parent_name} granted {name}, which has tools "
                    f"{sorted(_tools_of(name) - parent_tools)} the parent lacks",
                )

    def test_composite_preserves_its_own_name(self):
        expanded = _expand_parent_toolsets({COMPOSITE})
        self.assertIn(COMPOSITE, expanded)

    def test_individual_toolset_unchanged(self):
        """When parent already uses individual toolsets, expansion keeps them."""
        expanded = _expand_parent_toolsets({"web", "terminal"})
        self.assertIn("web", expanded)
        self.assertIn("terminal", expanded)

    def test_empty_parent_toolsets(self):
        expanded = _expand_parent_toolsets(set())
        self.assertEqual(expanded, set())

    def test_unknown_toolset_passthrough(self):
        """Unknown toolset names pass through without error."""
        expanded = _expand_parent_toolsets({"nonexistent-toolset-xyz"})
        self.assertIn("nonexistent-toolset-xyz", expanded)

    def test_intersection_with_expanded_composite(self):
        """End-to-end: a narrower toolset the composite covers survives the intersection."""
        parent_toolsets = {COMPOSITE}
        expanded = _expand_parent_toolsets(parent_toolsets)

        candidates = sorted(_contained_in(_tools_of(COMPOSITE)) - {COMPOSITE})
        self.assertTrue(candidates, f"{COMPOSITE} contains no narrower toolset to request")

        for requested in candidates:
            # mirrors handle_delegate's intersection expression
            child_toolsets = [t for t in [requested] if t in expanded]
            self.assertEqual(child_toolsets, [requested])


if __name__ == "__main__":
    unittest.main()
