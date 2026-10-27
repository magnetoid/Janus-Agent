"""The hosted one-line installers are reachable and self-consistent.

Every reference used to point at the repository's HTML page
(``https://github.com/magnetoid/janus``) as if it were a script, so
``curl … | bash`` / ``irm …`` could never have worked. These invariants pin:
scripts parse, every script-invocation snippet references a raw
``scripts/install.(sh|ps1)`` URL, and ``--tag`` pinning exists on both platforms.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_RAW_SH = re.compile(r"https://raw\.githubusercontent\.com/[^/\s]+/janus/[^\s]+/scripts/install\.sh$")
_RAW_PS1 = re.compile(r"https://raw\.githubusercontent\.com/[^/\s]+/janus/[^\s]+/scripts/install\.ps1$")
_HTML_PAGE = "https://github.com/magnetoid/janus"


def _doc_files() -> list[Path]:
    files = [_REPO / "README.md"]
    files += sorted((_REPO / "scripts").glob("install.*"))
    files += sorted((_REPO / "website" / "docs" / "getting-started").glob("*.md"))
    return [f for f in files if f.is_file()]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_install_sh_parses():
    subprocess.run(["bash", "-n", str(_REPO / "scripts" / "install.sh")], check=True)


def test_no_html_page_url_used_as_a_script():
    bad = []
    for f in _doc_files():
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"curl -fsSL (https?://\S+) \| bash", text):
            if m.group(1) == _HTML_PAGE:
                bad.append(f"{f.relative_to(_REPO)}: {m.group(0)}")
        for m in re.finditer(r"irm (\S+?)\)", text):
            if m.group(1) == _HTML_PAGE:
                bad.append(f"{f.relative_to(_REPO)}: {m.group(0)}")
        if f"-Uri '{_HTML_PAGE}'" in text:
            bad.append(f"{f.relative_to(_REPO)}: -Uri html page")
    assert bad == [], bad


def test_every_one_liner_targets_a_raw_install_script():
    seen = 0
    for f in _doc_files():
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"curl -fsSL (https?://\S+) \| bash", text):
            seen += 1
            assert _RAW_SH.match(m.group(1)), f"{f.name}: {m.group(1)}"
        for m in re.finditer(r"irm (\S+?)\)", text):
            seen += 1
            assert _RAW_PS1.match(m.group(1)), f"{f.name}: {m.group(1)}"
    assert seen > 0, "expected at least one install one-liner in README/docs"


def test_readme_leads_with_the_one_liners_not_source_install():
    text = (_REPO / "README.md").read_text(encoding="utf-8")
    assert "will follow" not in text.split("## 🎬", 1)[0]
    quick = text.split("## 🚀 Quick Install", 1)[1].split("## 🎬", 1)[0]
    first_clone = quick.find("git clone")
    first_curl = quick.find("curl -fsSL")
    assert first_curl != -1 and (first_clone == -1 or first_curl < first_clone)


def test_tag_pinning_exists_on_both_installers():
    sh = (_REPO / "scripts" / "install.sh").read_text(encoding="utf-8")
    ps1 = (_REPO / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert re.search(r"--tag\|-Tag\)", sh)
    assert re.search(r'git checkout --detach "refs/tags/\$INSTALL_TAG"', sh)
    assert "[string]$Tag" in ps1
