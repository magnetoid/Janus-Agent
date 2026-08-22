"""Tests for the ``janus prompt-size`` diagnostic (issue #34667)."""

import json

import pytest

from janus_cli.prompt_size import (
    _SKILLS_BLOCK_RE,
    compute_prompt_breakdown,
    render_breakdown,
)

# Overrides the global --timeout=30. Every test here builds a real AIAgent,
# and whichever one runs first absorbs the whole process bootstrap: importing
# the OpenAI SDK, the env-probe subprocesses, and the models.dev context-length
# fetch (a network round trip plus a multi-MB cache write into the per-test
# JANUS_HOME). That one-time cost alone lands within a few seconds of the 30s
# cap, so the first test flakes on a cold or slow runner while the remaining
# six take ~1s each. Raise the cap rather than let bootstrap latency decide
# whether the suite is green.
pytestmark = pytest.mark.timeout(120)


def _seed_memory(janus_home, memory_text="", user_text=""):
    mem_dir = janus_home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if memory_text:
        (mem_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
    if user_text:
        (mem_dir / "USER.md").write_text(user_text, encoding="utf-8")


def _seed_skill(janus_home, name, description):
    skill_dir = janus_home / "skills" / "demo" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nbody\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    janus_home = tmp_path / ".janus"
    janus_home.mkdir()
    monkeypatch.setenv("JANUS_HOME", str(janus_home))
    monkeypatch.chdir(tmp_path)  # avoid picking up the repo's AGENTS.md
    # The inspection agent only emits the ``<available_skills>`` block when
    # skills tools are in its schema, and tool modules no longer self-register
    # on ``import model_tools`` — real entry points (``janus_cli/main.py``,
    # which dispatches ``janus prompt-size``, plus ``cli.py`` and
    # ``gateway/runner.py``) call ``discover_builtin_tools()`` at startup.
    # Import just the skills tool module here: it registers exactly the
    # dependency these assertions have, without paying for every other tool's
    # check_fn probing Docker/Modal/playwright/provider endpoints.
    import tools.skills_tool  # noqa: F401
    return janus_home


def test_breakdown_keys_and_shape(isolated_home):
    """The breakdown exposes every documented key with int byte/char counts."""
    data = compute_prompt_breakdown("cli")
    assert set(data) >= {
        "platform",
        "model",
        "system_prompt",
        "skills_index",
        "memory",
        "user_profile",
        "tools",
        "sections",
    }
    assert data["platform"] == "cli"
    for key in ("system_prompt", "skills_index", "memory", "user_profile"):
        assert data[key]["bytes"] >= 0
        assert data[key]["chars"] >= 0
    assert data["tools"]["count"] >= 0
    assert data["tools"]["json_bytes"] >= 0
    # System prompt is non-trivial even with empty home (identity + guidance).
    assert data["system_prompt"]["bytes"] > 0


def test_runs_offline_without_credentials(isolated_home, monkeypatch):
    """No provider credentials configured → still produces a breakdown."""
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY",
                "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    data = compute_prompt_breakdown("cli")
    assert data["system_prompt"]["bytes"] > 0


def test_skills_index_reflects_installed_skills(isolated_home):
    """Installing a skill makes the skills-index block non-empty.

    Note: the skills prompt is cached per-process (in-process LRU + disk
    snapshot), so we seed the skill BEFORE the first build rather than
    comparing before/after within one process.
    """
    _seed_skill(isolated_home, "hello", "a demo skill for size testing")
    data = compute_prompt_breakdown("cli")
    assert data["skills_index"]["bytes"] > 0


def test_memory_and_profile_are_attributed(isolated_home):
    """Memory and user-profile blocks are measured separately."""
    _seed_memory(
        isolated_home,
        memory_text="Project uses pytest.\n",
        user_text="User is a developer.\n",
    )
    data = compute_prompt_breakdown("cli")
    assert data["memory"]["bytes"] > 0
    assert data["user_profile"]["bytes"] > 0


def test_skills_block_regex_matches_tagged_block():
    text = "preamble\n<available_skills>\n  cat:\n    - a: b\n</available_skills>\ntail"
    m = _SKILLS_BLOCK_RE.search(text)
    assert m is not None
    assert m.group(0).startswith("<available_skills>")
    assert m.group(0).endswith("</available_skills>")


def test_render_breakdown_is_plain_text(isolated_home):
    data = compute_prompt_breakdown("cli")
    out = render_breakdown(data)
    assert "System prompt total" in out
    assert "skills index" in out
    assert "Tool schemas" in out
    # Plain text — no JSON braces leaking in.
    assert not out.strip().startswith("{")


def test_json_serializable(isolated_home):
    data = compute_prompt_breakdown("cli")
    # Round-trips cleanly for ``--json`` output.
    assert json.loads(json.dumps(data)) == json.loads(json.dumps(data))
