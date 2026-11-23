"""DREAMS.md: a human-readable digest of every completed sleep cycle."""
import sys
from types import SimpleNamespace

import pytest

from agent import sleep
from tools.memory_tool import MemoryStore


def _fake_llm(reply: str = "[]"):
    def _caller(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    return _caller


@pytest.fixture
def busy_night(monkeypatch):
    """Every consolidation step reports something, without a model."""
    monkeypatch.setattr("agent.memory_gardener.reconcile",
                        lambda store, **k: {"removed": ["User lives in Berlin"]})
    monkeypatch.setattr(sleep, "prune_low_salience", lambda store, **k: ["tmp note about lunch"])
    monkeypatch.setattr(sleep, "synthesize_cross_session_lessons",
                        lambda **k: [{"lesson": "Run the migration before the tests."}])
    monkeypatch.setattr("agent.proposer.propose_skill_improvements",
                        lambda **k: {"proposed": ["si-0007"]})


@pytest.fixture
def quiet_night(monkeypatch):
    monkeypatch.setattr("agent.memory_gardener.reconcile", lambda store, **k: {"removed": []})
    monkeypatch.setattr(sleep, "prune_low_salience", lambda store, **k: [])
    monkeypatch.setattr(sleep, "synthesize_cross_session_lessons", lambda **k: [])
    monkeypatch.setattr("agent.proposer.propose_skill_improvements", lambda **k: {"proposed": []})


def _store():
    store = MemoryStore()
    store.load_from_disk()
    return store


def test_cycle_writes_what_it_changed_to_dreams(busy_night):
    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())

    text = sleep.dreams_path().read_text(encoding="utf-8")
    assert "User lives in Berlin" in text            # contradiction retired
    assert "tmp note about lunch" in text            # forgotten
    assert "Run the migration before the tests." in text  # lesson
    assert "si-0007" in text                         # proposal awaiting review


def test_dreams_lives_next_to_the_memory_files(busy_night):
    from tools.memory_tool import get_memory_dir

    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())

    assert sleep.dreams_path().parent == get_memory_dir()
    assert sleep.dreams_path().name == "DREAMS.md"


def test_dry_run_leaves_no_digest(busy_night):
    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm(), dry_run=True)

    assert not sleep.dreams_path().exists()


def test_quiet_cycle_still_records_that_it_ran(quiet_night):
    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())

    digest = sleep.read_dream_digest(cycles=1)
    assert len(digest) == 1
    assert "nothing to consolidate" in digest[0].lower()


def test_cycles_append_and_newest_is_read_back_first(busy_night, monkeypatch):
    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())
    monkeypatch.setattr(sleep, "synthesize_cross_session_lessons",
                        lambda **k: [{"lesson": "Second night lesson."}])
    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())

    everything = sleep.read_dream_digest(cycles=0)
    latest = sleep.read_dream_digest(cycles=1)
    assert len(everything) == 2
    assert latest == everything[:1]
    assert "Second night lesson." in latest[0]
    assert "Second night lesson." not in everything[1]


def test_long_entries_and_long_lists_are_kept_readable():
    report = {
        "graduated_facts": 0, "graduated_skills": 0, "reconciled": [],
        "pruned": [f"entry {i} " + "x" * 500 for i in range(40)], "lessons": [],
    }

    section = sleep.render_dream_digest(report, ts="2026-11-10T03:00:00")

    assert "x" * 500 not in section
    assert "entry 0" in section
    assert "entry 39" not in section
    assert "more" in section


def test_digest_can_be_turned_off(busy_night):
    from janus_constants import get_janus_home

    (get_janus_home() / "config.yaml").write_text("sleep:\n  dreams_digest: false\n", encoding="utf-8")

    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())

    assert not sleep.dreams_path().exists()
    assert sleep.sleep_log_path().exists()  # the counts log is unaffected


def test_unwritable_digest_never_breaks_the_cycle(busy_night, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(sleep, "dreams_path", lambda: blocker / "DREAMS.md")

    report = sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())

    assert report["error"] is None
    assert sleep.load_sleep_state().get("last_run")


def test_missing_digest_reads_as_empty():
    assert sleep.read_dream_digest(cycles=5) == []


def test_memory_log_digest_prints_the_latest_cycles(busy_night, monkeypatch, capsys):
    sleep.run_sleep_cycle(_store(), llm_caller=_fake_llm())
    monkeypatch.setattr(sys, "argv", ["janus", "memory", "log", "--digest"])

    from janus_cli.main import main

    main()

    assert "Run the migration before the tests." in capsys.readouterr().out


def test_memory_log_digest_without_cycles_explains_where_it_will_appear(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["janus", "memory", "log", "--digest"])

    from janus_cli.main import main

    main()

    assert "DREAMS.md" in capsys.readouterr().out
