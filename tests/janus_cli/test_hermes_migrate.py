"""Legacy (~/.hermes) -> Janus (~/.janus) migration."""
from pathlib import Path

from janus_cli import hermes_migrate as hm


def _make_hermes(root: Path) -> Path:
    src = root / ".hermes"
    (src / "memories").mkdir(parents=True)
    (src / "skills" / "my-skill").mkdir(parents=True)
    (src / "config.yaml").write_text(
        "model: hermes-coder-7b\nprovider: openrouter\npaths:\n  home: /home/u/.hermes\n",
        encoding="utf-8")
    (src / ".env").write_text("HERMES_HOME=/home/u/.hermes\nOPENAI_API_KEY=sk-x\n", encoding="utf-8")
    (src / "memories" / "MEMORY.md").write_text("- a durable fact\n", encoding="utf-8")
    return src


def test_looks_like_home_and_detection(tmp_path, monkeypatch):
    src = _make_hermes(tmp_path)
    assert hm.looks_like_home(src)
    assert not hm.looks_like_home(tmp_path / "nope")
    monkeypatch.setenv("HERMES_HOME", str(src))
    assert hm.legacy_hermes_home() == src


def test_plan_classifies_import_vs_conflict(tmp_path):
    src = _make_hermes(tmp_path)
    dst = tmp_path / ".janus"; dst.mkdir()
    (dst / "config.yaml").write_text("model: existing\n", encoding="utf-8")  # conflict
    plan = hm.plan_migration(src, dst)
    assert "config.yaml" in plan["conflict"]
    assert "memories" in plan["import"] and "skills" in plan["import"]
    assert ".env" in plan["import"]


def test_migrate_copies_nondestructive_and_rewrites(tmp_path):
    src = _make_hermes(tmp_path)
    dst = tmp_path / ".janus"
    res = hm.migrate(src, dst)
    # copied
    assert (dst / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "- a durable fact\n"
    assert (dst / "skills" / "my-skill").is_dir()
    # source untouched (non-destructive)
    assert (src / "config.yaml").exists()
    # token rewrite in config.yaml + .env
    cfg = (dst / "config.yaml").read_text(encoding="utf-8")
    env = (dst / ".env").read_text(encoding="utf-8")
    assert "/.janus" in cfg and "/.hermes" not in cfg
    assert "JANUS_HOME=" in env and "HERMES_HOME" not in env
    # model ID + provider slug PRESERVED (not brand-swept)
    assert "hermes-coder-7b" in cfg
    assert "provider: openrouter" in cfg
    assert "OPENAI_API_KEY=sk-x" in env
    assert "config.yaml" in res["rewritten"] and ".env" in res["rewritten"]


def test_migrate_skips_conflicts_unless_overwrite(tmp_path):
    src = _make_hermes(tmp_path)
    dst = tmp_path / ".janus"; dst.mkdir()
    (dst / "config.yaml").write_text("model: keep-me\n", encoding="utf-8")
    res = hm.migrate(src, dst)
    assert "config.yaml" in res["skipped"]
    assert (dst / "config.yaml").read_text(encoding="utf-8") == "model: keep-me\n"
    # overwrite replaces it (and then rewrites tokens)
    res2 = hm.migrate(src, dst, overwrite=True)
    assert "config.yaml" in res2["imported"]
    assert "hermes-coder-7b" in (dst / "config.yaml").read_text(encoding="utf-8")


def test_dry_run_makes_no_changes(tmp_path):
    src = _make_hermes(tmp_path)
    dst = tmp_path / ".janus"
    res = hm.migrate(src, dst, apply=False)
    assert "memories" in res["imported"]
    assert not dst.exists()  # dry run creates nothing


def test_missing_source_is_best_effort(tmp_path):
    res = hm.migrate(tmp_path / "nope", tmp_path / ".janus")
    assert res["errors"] and not res["imported"]


def test_rewrite_preserves_model_ids():
    text = "model: my-hermes-2\nft: Hermes-Coder-70B\nhome: /u/.hermes\nHERMES_HOME=/u/.hermes\n"
    out = hm._rewrite_brand_tokens(text)
    assert "my-hermes-2" in out            # model family preserved
    assert "Hermes-Coder-70B" in out       # model ID preserved
    assert "/.janus" in out and "/.hermes" not in out
    assert "JANUS_HOME=/u/.janus" in out


def test_remove_legacy(tmp_path):
    src = _make_hermes(tmp_path)
    assert hm.remove_legacy(src) is True
    assert not src.exists()


def test_offer_hermes_migration_first_run(tmp_path, monkeypatch):
    """The first-run wizard hook imports a detected ~/.hermes (declining cleanup)."""
    from janus_cli import setup as js
    from janus_constants import get_janus_home

    src = _make_hermes(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(src))
    # say yes to "Import…", no to "Delete the old…"
    monkeypatch.setattr(js, "prompt_yes_no", lambda msg, default=False: "import" in msg.lower())

    dst = get_janus_home()
    ran = js._offer_hermes_migration(dst)
    assert ran is True
    assert (dst / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "- a durable fact\n"
    assert "JANUS_HOME=" in (dst / ".env").read_text(encoding="utf-8")
    assert src.exists()  # cleanup declined → legacy home preserved


def test_offer_hermes_migration_absent(tmp_path, monkeypatch):
    """No legacy home → the hook is a no-op returning False."""
    from janus_cli import setup as js
    from janus_constants import get_janus_home

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "does-not-exist"))
    assert js._offer_hermes_migration(get_janus_home()) is False
