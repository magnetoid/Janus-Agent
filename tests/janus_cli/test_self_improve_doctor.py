"""`janus self-improve doctor` — one-screen loop diagnosis (read-only)."""
import os
import subprocess
import sys


def _run(home):
    env = dict(os.environ, JANUS_HOME=str(home))
    return subprocess.run(
        [sys.executable, "-m", "janus_cli.main", "self-improve", "doctor"],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_doctor_exits_zero_on_empty_home(tmp_path):
    home = tmp_path / ".janus"
    home.mkdir(parents=True)
    r = _run(home)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    # every broken precondition names itself and its next command
    assert "enabled=False" in out
    assert "eval suite" in out and "janus evals init" in out
    assert "NEVER closed" in out


def test_doctor_reports_draft_trial_state(tmp_path):
    home = tmp_path / ".janus"
    d = home / "skills" / ".drafts" / "myskill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: d\n---\n\nbody", encoding="utf-8")
    r = _run(home)
    assert r.returncode == 0, r.stderr
    assert "draft:myskill" in r.stdout
    assert "trial_drafts" in r.stdout   # the unblock hint names the flag
