"""No committed merge-conflict markers anywhere in tracked text files.

A stash-pop conflict was once committed into SECURITY.md (byte-identical sides,
so it was easy to miss). A file is flagged only when it contains BOTH a
``<<<<<<< `` opener and a ``>>>>>>> `` closer — a lone ``=======`` line is a
legitimate heading/separator in docstrings and must not trip this.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_TEXT = re.compile(r"\.(md|py|sh|ps1|yaml|yml|toml|ts|tsx|json)$")
_SKIP = ("uv.lock", "package-lock.json", "website/static/")
_OPEN = re.compile(r"^<<<<<<< ", re.M)
_CLOSE = re.compile(r"^>>>>>>> ", re.M)


def _tracked_text_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=_REPO, check=True,
                             capture_output=True, text=True).stdout
    except Exception:
        pytest.skip("git not available")
    files = []
    for rel in out.splitlines():
        if not _TEXT.search(rel) or any(s in rel for s in _SKIP):
            continue
        p = _REPO / rel
        if p.is_file():
            files.append(p)
    return files


def test_no_merge_conflict_markers_in_tracked_files():
    offenders = []
    for p in _tracked_text_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _OPEN.search(text) and _CLOSE.search(text):
            offenders.append(str(p.relative_to(_REPO)))
    assert offenders == [], f"merge-conflict markers committed in: {offenders}"


def test_security_md_is_well_formed():
    text = (_REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert "<<<<<<<" not in text and ">>>>>>>" not in text
    # The reporting contact survives the conflict resolution exactly once in §1.
    section = text.split("## 2.", 1)[0]
    assert section.count("security@cloud-industry.com") == 1
