"""Cron memory / continuity / disabled-toolset tests."""

from cron.jobs import create_job, save_job_output
from cron.scheduler import _build_job_prompt, _resolve_cron_disabled_toolsets


def test_memory_toolset_always_disabled_for_cron():
    disabled = _resolve_cron_disabled_toolsets({})
    assert "memory" in disabled
    assert "cronjob" in disabled


def test_continuity_injects_previous_run(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    job = create_job(prompt="Check inbox", schedule="every 1h")
    save_job_output(job["id"], "yesterday: 2 new replies")
    prompt = _build_job_prompt(job)
    assert "Previous run of this job" in prompt
    assert "yesterday: 2 new replies" in prompt
    assert "Check inbox" in prompt


def test_skip_memory_skips_continuity(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    job = create_job(prompt="Check inbox", schedule="every 1h", skip_memory=True)
    save_job_output(job["id"], "yesterday: 2 new replies")
    prompt = _build_job_prompt(job)
    assert "Previous run of this job" not in prompt
    assert "Check inbox" in prompt
