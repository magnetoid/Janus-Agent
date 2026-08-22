"""Tests for ruff lint config — guards against accidental rule removal.

PLW1514 (unspecified-encoding) was enabled after a debug session on
Windows turned up three separate UTF-8 regressions in execute_code.
The rule catches bare ``open()`` / ``read_text()`` / ``write_text()``
calls that default to locale encoding — cp1252 on Windows — which
silently corrupts non-ASCII content.

These tests ensure:
  1. PLW1514 stays in ``[tool.ruff.lint.select]``
  2. Some CI workflow still invokes ``ruff check`` in a *blocking* step
  3. pyproject.toml has ``preview = true`` (required — PLW1514 is a
     preview rule in ruff 0.15.x)

If someone removes any of these, CI stops enforcing UTF-8-explicit
opens and we're back to the original Windows-regression trap.
"""

from __future__ import annotations

import pathlib
import re

import pytest

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — 3.10 and earlier
    import tomli as tomllib  # type: ignore

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_pyproject() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


class TestRuffConfig:
    def test_plw1514_is_in_select_list(self):
        """pyproject.toml must keep PLW1514 in [tool.ruff.lint.select]."""
        cfg = _load_pyproject()
        selected = (
            cfg.get("tool", {})
            .get("ruff", {})
            .get("lint", {})
            .get("select", [])
        )
        assert "PLW1514" in selected, (
            "PLW1514 (unspecified-encoding) was removed from "
            "[tool.ruff.lint.select].  This rule blocks bare open() calls "
            "that default to locale encoding on Windows — removing it "
            "re-opens a class of UTF-8 bugs we already paid to close.  "
            "If you genuinely want to remove it, delete this test in the "
            "same commit so the intent is deliberate."
        )

    def test_preview_mode_enabled(self):
        """PLW1514 is a preview rule in ruff 0.15.x — preview=true is
        required for it to actually run."""
        cfg = _load_pyproject()
        ruff_cfg = cfg.get("tool", {}).get("ruff", {})
        assert ruff_cfg.get("preview") is True, (
            "[tool.ruff] preview=true is required — PLW1514 is a preview "
            "rule and silently becomes a no-op without it.  If this ever "
            "becomes a stable rule, you can drop preview=true but must "
            "verify PLW1514 still fires in a sample test run first."
        )


WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

_RUFF_CHECK_RE = re.compile(r"(^|[\s/\\])ruff\s+check\b")


def _iter_workflows():
    """Yield ``(path, parsed_yaml)`` for every GitHub Actions workflow."""
    import yaml

    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        content = path.read_text(encoding="utf-8")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:  # pragma: no cover - only on a broken file
            pytest.fail(f"{path.name} is not valid YAML: {exc}")
        yield path, parsed


def _blocking_ruff_steps():
    """Return ``[(workflow, job, run_command)]`` for blocking ruff steps.

    A step counts as *blocking* when a ruff-check failure fails the job:
      * the command invokes ``ruff check`` (however ruff is spelled — bare,
        ``.venv/bin/ruff``, ``uv run ruff`` …)
      * it is not neutered by ``--exit-zero`` or ``|| true``
      * neither the step nor its job is ``continue-on-error: true``
        (an informational job cannot enforce anything)
    """
    found = []
    for path, parsed in _iter_workflows():
        if not isinstance(parsed, dict):
            continue
        for job_name, job in (parsed.get("jobs") or {}).items():
            if not isinstance(job, dict) or job.get("continue-on-error"):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict) or step.get("continue-on-error"):
                    continue
                run = step.get("run")
                if not isinstance(run, str) or not _RUFF_CHECK_RE.search(run):
                    continue
                if "--exit-zero" in run or "|| true" in run:
                    continue
                found.append((path.name, job_name, run.strip()))
    return found


class TestLintWorkflow:
    """CI must actually enforce the ruff config above.

    This used to pin ``.github/workflows/lint.yml`` by name; the lint job
    was later merged into ``tests.yml`` and the tests broke even though the
    enforcement they guard never went away.  The invariant is "*some*
    non-informational workflow job runs ``ruff check`` in a way that can
    fail the build" — not which file that job lives in.
    """

    def test_workflows_are_valid_yaml(self):
        """Every workflow must parse and declare jobs — a broken CI config
        silently stops enforcing everything below it."""
        seen = 0
        for path, parsed in _iter_workflows():
            assert isinstance(parsed, dict), f"{path.name}: not a YAML mapping"
            assert parsed.get("jobs"), f"{path.name}: no jobs defined"
            seen += 1
        assert seen, f"no GitHub Actions workflows found under {WORKFLOWS_DIR}"

    def test_some_workflow_runs_blocking_ruff_check(self):
        """A ruff violation must be able to fail CI.

        ``ruff check`` behind ``--exit-zero``/``|| true``, or inside a
        ``continue-on-error`` job, reports violations without blocking —
        which is how the PLW1514 guarantee quietly evaporates.
        """
        blocking = _blocking_ruff_steps()
        assert blocking, (
            "No workflow under .github/workflows runs a blocking "
            "``ruff check`` step (one without --exit-zero, not masked by "
            "|| true, in a job that is not continue-on-error).  Restore it "
            "— the PLW1514 rule is only useful if CI actually fails on "
            "violation."
        )

    def test_blocking_ruff_check_covers_the_whole_repo(self):
        """The blocking invocation must lint everything, not one subtree.

        ``ruff check janus_cli/`` would pass this file's other test while
        leaving most bare ``open()`` calls unchecked.  Accept an explicit
        ``.``/repo-root target or no target at all (ruff defaults to the
        current directory).
        """
        repo_wide = []
        for workflow, job, run in _blocking_ruff_steps():
            for line in run.splitlines():
                if not _RUFF_CHECK_RE.search(line):
                    continue
                args = line.split("check", 1)[1].split()
                targets = [a for a in args if not a.startswith("-")]
                if not targets or "." in targets:
                    repo_wide.append(f"{workflow}:{job}")
        assert repo_wide, (
            "A blocking ``ruff check`` exists but none of them lint the "
            f"whole repo. Found: {_blocking_ruff_steps()!r}"
        )
