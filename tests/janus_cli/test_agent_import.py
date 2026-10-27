"""Importing another assistant's setup (janus_cli/agent_import.py).

Invariants: the plan lists exactly the importable items and classifies
duplicates; a preview creates nothing; apply adds exactly the new memory
entries, copies scanned skills, adds only secret-free MCP servers; credentials
are NEVER opened; the source is untouched; the import is idempotent.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from janus_cli import agent_import as ai
from janus_cli.import_common import ENTRY_DELIMITER


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture()
def claude_home(tmp_path, monkeypatch):
    root = tmp_path / ".claude"
    root.mkdir()
    (root / "CLAUDE.md").write_text(
        "# Preferences\n\n- Always use type hints\n- Prefer uv over pip\n- Run tests before pushing\n",
        encoding="utf-8")
    (root / "skills" / "deploy-helper").mkdir(parents=True)
    (root / "skills" / "deploy-helper" / "SKILL.md").write_text(
        "---\nname: deploy-helper\ndescription: Deploys things.\n---\nbody\n", encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / "review.md").write_text("# Review\nReview the diff carefully.\n", encoding="utf-8")
    (root / ".credentials.json").write_text('{"token": "SECRET"}', encoding="utf-8")
    (root / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(rm:*)"]}}), encoding="utf-8")
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {
        "safe": {"command": "npx", "args": ["-y", "safe-server"]},
        "secretive": {"command": "npx", "args": ["x"], "env": {"API_KEY": "shh"}},
    }}), encoding="utf-8")

    janus_home = tmp_path / "janus"
    (janus_home / "memories").mkdir(parents=True)
    (janus_home / "memories" / "MEMORY.md").write_text(
        "Preferences: Prefer uv over pip", encoding="utf-8")   # one duplicate of CLAUDE.md
    monkeypatch.setattr("janus_constants.get_janus_home", lambda: janus_home)
    monkeypatch.setattr(ai, "_memory_limit_from_config", lambda: 2200)
    monkeypatch.setattr(ai, "_scan_ok", lambda stage: (True, "ok"))
    saved: dict = {}
    monkeypatch.setattr("janus_cli.mcp_config._get_mcp_servers", lambda config=None: dict(saved))
    monkeypatch.setattr("janus_cli.mcp_config._save_mcp_server", lambda n, c: saved.__setitem__(n, c))
    return root, janus_home, saved


def test_plan_lists_importable_items_and_advisories(claude_home):
    root, _, _ = claude_home
    pl = ai.plan("claude-code", root)
    assert len(pl.memory_entries) == 3
    assert {p.name for p in pl.skills} == {"deploy-helper", "review.md"}
    assert set(pl.mcp_servers) == {"safe", "secretive"}
    assert any("permissions.allow" in a for a in pl.advisories)
    assert any("Credentials are never imported" in a for a in pl.advisories)


def test_preview_creates_nothing(claude_home):
    root, janus_home, saved = claude_home
    before = _tree_hash(janus_home)
    res = ai.migrate(ai.plan("claude-code", root), janus_home, apply=False)
    assert res["memory_added"] == 2 and res["memory_duplicates"] == 1
    assert _tree_hash(janus_home) == before and saved == {}


def test_apply_imports_exactly_the_new_items(claude_home):
    root, janus_home, saved = claude_home
    src_before = _tree_hash(root)
    res = ai.migrate(ai.plan("claude-code", root), janus_home, apply=True)

    entries = (janus_home / "memories" / "MEMORY.md").read_text(encoding="utf-8").split(ENTRY_DELIMITER)
    assert len(entries) == 3 and res["memory_added"] == 2          # 1 existing + 2 new, duplicate skipped
    assert (janus_home / "skills" / "imported" / "deploy-helper" / "SKILL.md").is_file()
    assert (janus_home / "skills" / "imported" / "review" / "SKILL.md").is_file()
    assert set(saved) == {"safe"}                                  # secret-bearing server skipped
    assert any("secrets" in s["reason"] for s in res["mcp_skipped"])
    assert _tree_hash(root) == src_before                          # source untouched


def test_credentials_are_never_opened(claude_home, monkeypatch):
    root, janus_home, _ = claude_home
    real = Path.read_text

    def _guard(self, *a, **k):
        assert self.name not in {".credentials.json", "auth.json"}, f"opened {self}"
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", _guard)
    ai.migrate(ai.plan("claude-code", root), janus_home, apply=True)


def test_migrate_secrets_flag_imports_the_secret_server(claude_home):
    root, janus_home, saved = claude_home
    ai.migrate(ai.plan("claude-code", root), janus_home, apply=True, migrate_secrets=True)
    assert set(saved) == {"safe", "secretive"}


def test_scanner_rejection_blocks_the_skill(claude_home, monkeypatch):
    root, janus_home, _ = claude_home
    monkeypatch.setattr(ai, "_scan_ok", lambda stage: (False, "malicious pattern"))
    res = ai.migrate(ai.plan("claude-code", root), janus_home, apply=True)
    assert res["skills"] == []
    assert not (janus_home / "skills" / "imported").exists()
    assert all("blocked by scanner" in s["reason"] for s in res["skills_blocked"])


def test_import_is_idempotent(claude_home):
    root, janus_home, saved = claude_home
    ai.migrate(ai.plan("claude-code", root), janus_home, apply=True)
    second = ai.migrate(ai.plan("claude-code", root), janus_home, apply=True)
    assert second["memory_added"] == 0
    assert second["skills"] == [] and all("already exists" in s["reason"] for s in second["skills_blocked"])
    assert second["mcp_added"] == [] and set(saved) == {"safe"}


def test_detect_honours_override_and_absence(tmp_path):
    assert ai.detect("claude-code", tmp_path / "missing") is None
    (tmp_path / "x").mkdir()
    assert ai.detect("claude-code", tmp_path / "x") == tmp_path / "x"
    assert ai.detect("not-a-source") is None


def test_codex_plan_reads_toml_mcp_and_never_auth(tmp_path):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "AGENTS.md").write_text("- Be terse\n", encoding="utf-8")
    (root / "config.toml").write_text(
        'model = "gpt-x"\n[mcp_servers.fs]\ncommand = "npx"\nargs = ["fs"]\n', encoding="utf-8")
    (root / "auth.json").write_text("{}", encoding="utf-8")
    pl = ai.plan("codex", root)
    assert pl.memory_entries == ["Be terse"]
    assert set(pl.mcp_servers) == {"fs"}
    assert any("auth.json" in a for a in pl.advisories)
    assert any("not imported" in n for n in pl.notes)
