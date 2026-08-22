"""`janus learning enable|disable` — the read-only closed-loop preset.

End-to-end through the real CLI entry (subprocess, isolated JANUS_HOME):
enable flips exactly the read-only flags and leaves the write-side
(auto_promote, playbook, dialectic) off; disable flips them back.
"""
import os
import subprocess
import sys

import yaml


def _run(args, home):
    env = dict(os.environ, JANUS_HOME=str(home))
    return subprocess.run(
        [sys.executable, "-m", "janus_cli.main", "learning", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def _cfg(home):
    p = home / "config.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_learning_enable_disable_preset(tmp_path):
    home = tmp_path / ".janus"
    home.mkdir(parents=True)

    r = _run(["enable"], home)
    assert r.returncode == 0, r.stderr
    assert "Learning loop preset → ON" in r.stdout
    cfg = _cfg(home)
    assert cfg["learning"]["track_outcomes"] is True
    assert cfg["learning"]["governor"]["enabled"] is True
    assert cfg["evals"]["trend"]["enabled"] is True
    # Memory mining rides the preset so "remembers you" holds on stock installs.
    assert cfg["memory"]["session_mining"] is True
    # Write-side stays untouched (defaults off) — the preset is read-only.
    assert not (cfg["learning"].get("governor") or {}).get("auto_promote")
    assert not (cfg["learning"].get("playbook") or {}).get("enabled")
    assert not (cfg["learning"].get("dialectic") or {}).get("enabled")

    r = _run(["disable"], home)
    assert r.returncode == 0, r.stderr
    cfg = _cfg(home)
    assert cfg["learning"]["track_outcomes"] is False
    assert cfg["learning"]["governor"]["enabled"] is False
    assert cfg["evals"]["trend"]["enabled"] is False
    assert cfg["memory"]["session_mining"] is False
