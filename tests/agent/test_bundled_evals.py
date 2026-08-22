"""Invariants over the committed bundled eval suite (repo ``evals/``).

Relationship assertions only — no spec-count snapshots (per the
no-change-detector-tests policy): the suite must parse, carry valid kinds,
tag capability specs for the time-horizon KPI, and be large enough to satisfy
the promotion gate's ``min_eval_specs`` floor.
"""
from pathlib import Path

import pytest

from agent.evals import load_eval_specs

REPO_EVALS = Path(__file__).resolve().parents[2] / "evals"

KNOWN_CHECKS = {"contains", "not_contains", "regex", "min_length",
                "max_length", "tool_called", "tool_not_called"}


@pytest.fixture(scope="module")
def specs():
    assert REPO_EVALS.is_dir(), "committed evals/ directory is missing"
    return load_eval_specs(REPO_EVALS)


def test_suite_parses_and_names_are_unique(specs):
    # load_eval_specs raises on duplicates — reaching here proves uniqueness.
    assert specs
    assert all(s.name for s in specs)


def test_kinds_are_valid_and_both_present(specs):
    kinds = {s.kind for s in specs}
    assert kinds <= {"regression", "capability"}
    assert "regression" in kinds and "capability" in kinds


def test_capability_specs_feed_the_horizon_kpi(specs):
    for s in specs:
        if s.kind == "capability":
            assert s.est_minutes > 0, f"{s.name} lacks est_minutes"


def test_every_check_type_is_known(specs):
    for s in specs:
        assert s.checks, f"{s.name} has no checks"
        for c in s.checks:
            assert c.get("type") in KNOWN_CHECKS, f"{s.name}: {c.get('type')}"


def test_suite_satisfies_the_promotion_floor():
    from janus_cli.config import DEFAULT_CONFIG
    floor = int(DEFAULT_CONFIG["learning"]["self_improve"]["min_eval_specs"])
    assert len(load_eval_specs(REPO_EVALS)) >= floor


def test_specs_are_hermetic_by_default(specs):
    # Bundled specs must not opt into live memory/context — the isolated
    # eval home has neither, and promotion comparisons need hermetic runs.
    for s in specs:
        assert not s.use_memory, f"{s.name} sets use_memory"
        assert not s.use_context_files, f"{s.name} sets use_context_files"
