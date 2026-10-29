"""`janus backup` encryption, secret exclusion and retention (plan item 3.3).

`age` / `gpg` are replaced by a stub on PATH that "encrypts" by prefixing a
marker, so the tests check the plumbing (argv, files left behind, restore)
without needing real keys.
"""
import os
import stat
import sys
import textwrap
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

MARK = b"FAKE-ENCRYPTED\n"

_STUB = textwrap.dedent(f"""\
    #!{sys.executable}
    import sys
    args = sys.argv[1:]
    out = args[args.index("-o" if "-o" in args else "--output") + 1]
    src = args[-1]
    data = open(src, "rb").read()
    mark = {MARK!r}
    decrypt = "-d" in args or "--decrypt" in args
    if decrypt:
        assert data.startswith(mark), "not produced by this stub"
        if "-i" in args:
            open(args[args.index("-i") + 1]).read()   # identity must exist
        data = data[len(mark):]
    else:
        assert ("-r" in args or "-R" in args or "--recipient" in args), "no recipient"
        data = mark + data
    open(out, "wb").write(data)
    """)


def _make_home(root: Path) -> None:
    (root / "config.yaml").write_text("model:\n  provider: openrouter\n")
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-test-123\n")
    (root / "auth.json").write_text("{}")
    (root / "skills" / "s").mkdir(parents=True)
    (root / "skills" / "s" / "SKILL.md").write_text("# S\n")
    (root / "profiles" / "coder").mkdir(parents=True)
    (root / "profiles" / "coder" / ".env").write_text("ANTHROPIC_API_KEY=x\n")
    (root / "profiles" / "coder" / "config.yaml").write_text("model: {}\n")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    janus_home = tmp_path / ".janus"
    janus_home.mkdir()
    _make_home(janus_home)
    monkeypatch.setenv("JANUS_HOME", str(janus_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return janus_home


@pytest.fixture()
def stub_bin(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("age", "gpg"):
        exe = bindir / name
        exe.write_text(_STUB)
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


def _backup(out, **kw):
    from janus_cli.backup import run_backup
    base = dict(output=str(out), encrypt=None, recipient=None, exclude_secrets=False, keep=None)
    base.update(kw)
    run_backup(Namespace(**base))


def _backups_in(d: Path):
    return sorted(p.name for p in d.iterdir() if p.name.startswith("janus-backup-"))


@pytest.mark.parametrize("tool", ["age", "gpg"])
def test_encrypted_backup_leaves_no_plaintext_and_decrypts_to_a_valid_zip(home, stub_bin, tmp_path, tool):
    dest = tmp_path / "dest"
    dest.mkdir()
    _backup(dest, encrypt=tool, recipient="age1example")
    names = _backups_in(dest)
    assert len(names) == 1 and names[0].endswith(f".zip.{tool}")
    assert not any(n.endswith(".zip") for n in os.listdir(dest))
    blob = (dest / names[0]).read_bytes()
    assert blob.startswith(MARK)
    plain = tmp_path / "plain.zip"
    plain.write_bytes(blob[len(MARK):])
    with zipfile.ZipFile(plain) as zf:
        assert zf.testzip() is None
        assert "config.yaml" in zf.namelist()


def test_missing_encryption_tool_fails_before_writing_anything(home, tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(SystemExit) as exc:
        _backup(dest, encrypt="age", recipient="age1example")
    assert exc.value.code not in (0, None)
    assert "age" in capsys.readouterr().out
    assert list(dest.iterdir()) == []


def test_encryption_needs_a_recipient(home, stub_bin, tmp_path, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(SystemExit):
        _backup(dest, encrypt="age")
    assert "recipient" in capsys.readouterr().out.lower()
    assert list(dest.iterdir()) == []


def test_exclude_secrets_drops_env_and_auth_files_everywhere(home, tmp_path):
    out = tmp_path / "b.zip"
    _backup(out, exclude_secrets=True)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "config.yaml" in names and "skills/s/SKILL.md" in names
    assert not {n for n in names if Path(n).name in {".env", "auth.json"}}


def test_keep_prunes_only_the_oldest_backups_after_success(home, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    for i, name in enumerate(["janus-backup-2026-01-01-000000.zip",
                              "janus-backup-2026-01-02-000000.zip.age",
                              "janus-backup-2026-01-03-000000.zip"]):
        p = dest / name
        p.write_bytes(b"old")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    unrelated = dest / "notes.txt"
    unrelated.write_text("keep me")
    _backup(dest, keep=2)
    left = _backups_in(dest)
    assert len(left) == 2
    assert "janus-backup-2026-01-03-000000.zip" in left        # newest old one survives
    assert unrelated.read_text() == "keep me"


def test_config_supplies_encryption_defaults(home, stub_bin, tmp_path):
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\nbackup:\n  encrypt: age\n  recipient: age1cfg\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    _backup(dest)
    assert _backups_in(dest)[0].endswith(".zip.age")


def test_import_decrypts_an_encrypted_backup(home, stub_bin, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    _backup(dest, encrypt="age", recipient="age1example")
    archive = dest / _backups_in(dest)[0]
    (home / "skills" / "s" / "SKILL.md").write_text("changed\n")
    identity = tmp_path / "key.txt"
    identity.write_text("AGE-SECRET-KEY-1FAKE\n")

    from janus_cli.backup import run_import
    run_import(Namespace(zipfile=str(archive), force=True, identity=str(identity)))
    assert (home / "skills" / "s" / "SKILL.md").read_text() == "# S\n"


def test_import_of_age_backup_without_identity_is_a_clear_error(home, stub_bin, tmp_path, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    _backup(dest, encrypt="age", recipient="age1example")
    archive = dest / _backups_in(dest)[0]
    from janus_cli.backup import run_import
    with pytest.raises(SystemExit):
        run_import(Namespace(zipfile=str(archive), force=True, identity=None))
    assert "--identity" in capsys.readouterr().out
