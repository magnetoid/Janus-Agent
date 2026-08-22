"""CLI subcommand: `janus evals <subcommand>`.

Thin shell around agent/evals.py. Lists specs, scaffolds an example,
runs the suite, and shows the latest results.

This module intentionally has no side effects at import time — main.py wires
the argparse subparsers on demand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _resolve_path(args) -> Optional[Path]:
    raw = getattr(args, "path", None)
    return Path(raw) if raw else None


def _cmd_list(args) -> int:
    from agent.evals import evals_dir, load_eval_specs

    try:
        specs = load_eval_specs(_resolve_path(args))
    except FileNotFoundError:
        print(
            f"evals: no specs found (looked in {_resolve_path(args) or evals_dir()}).\n"
            "Create one with `janus evals init`."
        )
        return 1
    except ValueError as exc:
        print(f"evals: bad spec — {exc}", file=sys.stderr)
        return 2

    if not specs:
        print("evals: no specs found. Create one with `janus evals init`.")
        return 1
    print(f"{len(specs)} eval(s):")
    for spec in specs:
        print(f"  {spec.name:40s} checks={len(spec.checks)}  ({spec.source_file})")
    return 0


def _cmd_init(args) -> int:
    from agent.evals import evals_dir, scaffold_starters

    written = scaffold_starters(force=bool(getattr(args, "force", False)))
    try:
        from tools.evals_sync import sync_evals

        synced = sync_evals(quiet=True)
        seeded = synced.get("copied", []) + synced.get("updated", [])
        if seeded:
            print(f"evals: seeded {len(seeded)} bundled spec file(s):")
            for name in seeded:
                print(f"  {evals_dir() / name}")
    except Exception:
        pass
    if written:
        print(f"evals: wrote {len(written)} starter spec(s):")
        for path in written:
            print(f"  {path}")
    else:
        print(f"evals: starter specs already present in {evals_dir()} (use --force to rewrite)")
    print("Edit them, then run `janus evals run`.")
    print("The dialectic pair needs the moa toolset: janus evals run --filter dialectic")
    return 0


def _cmd_run(args) -> int:
    from agent.evals import load_eval_specs, run_evals

    try:
        specs = load_eval_specs(_resolve_path(args))
    except FileNotFoundError:
        print("evals: no specs found. Create one with `janus evals init`.")
        return 1
    except ValueError as exc:
        print(f"evals: bad spec — {exc}", file=sys.stderr)
        return 2

    name_filter = getattr(args, "filter", None)
    if name_filter:
        specs = [s for s in specs if name_filter in s.name]
        if not specs:
            print(f"evals: no spec matches filter {name_filter!r}")
            return 1

    model = getattr(args, "model", None)
    if model:
        for spec in specs:
            spec.model = model

    print(f"running {len(specs)} eval(s)...")
    summary = run_evals(specs, on_progress=print)

    print(f"\n{summary['passed']}/{summary['total']} passed")
    for r in summary["results"]:
        if r["passed"]:
            continue
        print(f"\nFAIL {r['name']}" + (f" — error: {r['error']}" if r["error"] else ""))
        for c in r["checks"]:
            if not c["passed"]:
                print(f"  ✗ {c['type']}: {c['detail']}")
    if summary.get("results_path"):
        print(f"\nresults saved: {summary['results_path']}")
    return 0 if summary["failed"] == 0 else 1


def _cmd_results(args) -> int:
    from agent.evals import results_dir

    out_dir = results_dir()
    files = sorted(out_dir.glob("*.jsonl")) if out_dir.exists() else []
    if not files:
        print("evals: no saved results yet — run `janus evals run` first")
        return 1
    latest = files[-1]
    print(f"latest results ({latest.name}):")
    passed = failed = 0
    for line in latest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        ok = r.get("passed")
        passed += 1 if ok else 0
        failed += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {r.get('name')}  ({r.get('duration_s')}s)")
    print(f"\n{passed} passed, {failed} failed")
    return 0


def _cmd_trend(args) -> int:
    from agent import eval_trend as et

    rec = et.run_trend(path=_resolve_path(args))
    if rec.get("error"):
        print(f"evals: trend run failed — {rec['error']}", file=sys.stderr)
        return 1
    print(f"trend point: pass_rate={rec['pass_rate']} "
          f"({rec['passed']}/{rec['total']}) suite={rec['suite_hash']}")
    curve = et.learning_curve()
    if curve["learned"]:
        print(f"  learned since first run: {', '.join(curve['learned'])}")
    if curve["regressed"]:
        print(f"  REGRESSED since first run: {', '.join(curve['regressed'])}")
    return 0


def _cmd_gate(args) -> int:
    """Exit non-zero if the eval suite regressed — a CI/cron regression gate."""
    from agent import eval_trend as et

    fail_closed = bool(getattr(args, "fail_closed", False))
    if getattr(args, "run", False):
        rec = et.run_trend(path=_resolve_path(args))
        if rec.get("error"):
            print(f"evals: trend run failed — {rec['error']}", file=sys.stderr)
            return 2
    gate = et.regression_gate(window=getattr(args, "window", None),
                              fail_closed=fail_closed)
    print(gate["message"])
    if gate.get("pass_rate") is not None:
        jump = gate.get("jump")
        jump_s = f" jump={jump:+.4f}" if isinstance(jump, (int, float)) else ""
        print(f"  pass_rate={gate['pass_rate']} suite={gate.get('suite_hash')}{jump_s}")
    return 0 if gate["ok"] else 1


def _fmt_minutes(m) -> str:
    if not m:
        return "—"
    m = float(m)
    if m < 60:
        return f"{m:g}m"
    if m < 1440:
        return f"{m / 60:g}h"
    return f"{m / 1440:g}d"


def _cmd_horizon(args) -> int:
    """Show the METR-style time-horizon KPI over recorded trend points."""
    from agent import eval_trend as et

    lc = et.learning_curve()
    pts = [p for p in lc.get("points", []) if p.get("horizon_minutes") is not None]
    if not pts:
        print("evals: no horizon data yet. Tag evals with `est_minutes:` and run "
              "`janus evals trend` (or `gate --run`).")
        return 0
    first, last = pts[0]["horizon_minutes"], pts[-1]["horizon_minutes"]
    print(f"time horizon (longest task passed >=50% of the time): "
          f"{_fmt_minutes(last)}")
    if len(pts) >= 2:
        delta = (last or 0) - (first or 0)
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        print(f"  {arrow} {_fmt_minutes(first)} → {_fmt_minutes(last)} "
              f"over {len(pts)} trend point(s)")
    return 0


def _cmd_ab(args) -> int:
    from agent import eval_trend as et

    out = et.compare_feature(args.flag, path=_resolve_path(args))
    if out.get("error"):
        print(f"evals: ab failed — {out['error']}", file=sys.stderr)
        return 1
    print(f"A/B {out['flag']}: ON={out['pass_rate_on']} OFF={out['pass_rate_off']} "
          f"delta={out['delta']:+.4f}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring (called from janus_cli.main)
# ---------------------------------------------------------------------------

def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach `evals` subcommands to *parent*.

    main.py calls this with the ArgumentParser returned by
    ``subparsers.add_parser("evals", ...)``.
    """
    parent.set_defaults(func=lambda a: (parent.print_help(), 0)[1])
    subs = parent.add_subparsers(dest="evals_command")

    p_run = subs.add_parser("run", help="Run eval specs and report pass/fail")
    p_run.add_argument(
        "path", nargs="?", default=None,
        help="Spec file or directory (default: $JANUS_HOME/evals/)",
    )
    p_run.add_argument(
        "--filter", default=None,
        help="Only run evals whose name contains this substring",
    )
    p_run.add_argument(
        "--model", default=None,
        help="Override the model for every eval in this run",
    )
    p_run.set_defaults(func=_cmd_run)

    p_list = subs.add_parser("list", help="List discovered eval specs")
    p_list.add_argument(
        "path", nargs="?", default=None,
        help="Spec file or directory (default: $JANUS_HOME/evals/)",
    )
    p_list.set_defaults(func=_cmd_list)

    p_init = subs.add_parser("init", help="Scaffold an example eval spec")
    p_init.add_argument(
        "--force", action="store_true",
        help="Overwrite the example spec if it already exists",
    )
    p_init.set_defaults(func=_cmd_init)

    p_results = subs.add_parser("results", help="Show the most recent saved results")
    p_results.set_defaults(func=_cmd_results)

    p_trend = subs.add_parser("trend", help="Run the suite and record a learning-curve point")
    p_trend.add_argument("--path", help="Spec file or directory (default $JANUS_HOME/evals/)")
    p_trend.set_defaults(func=_cmd_trend)

    p_gate = subs.add_parser(
        "gate", help="Exit non-zero if the eval suite regressed — for CI / cron")
    p_gate.add_argument("--run", action="store_true",
                        help="Run the suite first (record a trend point), then gate")
    p_gate.add_argument("--path", help="Spec file or directory (default $JANUS_HOME/evals/)")
    p_gate.add_argument("--window", type=int, default=None,
                        help="Only compare the last N trend points")
    p_gate.add_argument("--fail-closed", action="store_true", dest="fail_closed",
                        help="Also fail on missing history or gate errors (CI mode)")
    p_gate.set_defaults(func=_cmd_gate)

    p_hz = subs.add_parser(
        "horizon", help="Show the METR-style time-horizon KPI over trend points")
    p_hz.set_defaults(func=_cmd_horizon)

    p_ab = subs.add_parser("ab", help="A/B a feature flag: suite pass-rate ON vs OFF")
    p_ab.add_argument("flag", help="section.key flag, e.g. memory.write_time_reconcile")
    p_ab.add_argument("--path", help="Spec file or directory (default $JANUS_HOME/evals/)")
    p_ab.set_defaults(func=_cmd_ab)


def cli_main(argv=None) -> int:
    """Standalone entry (also usable by janus_cli.main fallthrough)."""
    parser = argparse.ArgumentParser(prog="janus evals")
    register_cli(parser)
    args = parser.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        parser.print_help()
        return 0
    return int(fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli_main())
