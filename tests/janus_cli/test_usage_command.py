"""`janus usage` end to end: ledger rows in, grouped report out."""

import json
from argparse import Namespace

from agent import cost_ledger
from janus_cli import usage_report


def _args(**kw):
    base = {"by": "model", "days": 30, "source": None, "limit": 25, "as_json": False}
    base.update(kw)
    return Namespace(**base)


def _seed():
    cost_ledger.record_turn("s1", "opus", input_tokens=1000, output_tokens=200, cost_usd=0.40, status="actual")
    cost_ledger.record_turn("s1", "opus", input_tokens=500, output_tokens=100, cost_usd=0.10, status="actual")
    cost_ledger.record_turn("s2", "mystery", input_tokens=300, output_tokens=50, cost_usd=0.0, status="")
    cost_ledger.record_turn("cron_ab12cd34ef56_20261029_090000", "opus", input_tokens=100,
                            output_tokens=10, cost_usd=0.05, status="actual")


def test_report_shows_priced_and_unpriced_models(capsys):
    _seed()
    assert usage_report.cmd_usage(_args()) == 0
    out = capsys.readouterr().out
    opus = next(line for line in out.splitlines() if line.strip().startswith("opus"))
    assert "$0.55" in opus
    mystery = next(line for line in out.splitlines() if line.strip().startswith("mystery"))
    assert mystery.rstrip().endswith("n/a")
    assert "couldn't be priced" in out


def test_json_groups_reconcile(capsys):
    _seed()
    usage_report.cmd_usage(_args(by="job", as_json=True))
    report = json.loads(capsys.readouterr().out)
    assert report["by"] == "job"
    assert sum(g["turns"] for g in report["groups"]) == report["turns"] == 4
    keys = {g["key"] for g in report["groups"]}
    assert keys == {"ab12cd34ef56", "(interactive)"}


def test_empty_ledger_says_so(capsys):
    usage_report.cmd_usage(_args(by="day"))
    assert "No recorded turns" in capsys.readouterr().out


def test_janus_usage_runs_from_the_cli():
    import os
    import subprocess
    import sys
    _seed()
    r = subprocess.run([sys.executable, "-m", "janus_cli.main", "usage", "--by", "session", "--json"],
                       capture_output=True, text=True, timeout=120, env=os.environ.copy())
    assert r.returncode == 0, r.stderr[-2000:]
    report = json.loads(r.stdout[r.stdout.index("{"):])
    assert report["by"] == "session" and report["turns"] == 4
