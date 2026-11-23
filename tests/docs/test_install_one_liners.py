"""Install one-liners must fetch the install script, not the repo's web page.

``curl … | bash`` and ``iex (irm …)`` run whatever the URL returns. Pointed
at ``https://github.com/<owner>/<repo>`` they get the HTML page, which bash
and PowerShell can only choke on, so every copy-paste install from the docs,
the CLI hints and the bundled skill failed.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEARCHED = ["README.md", "website/docs", "website/i18n", "janus_cli", "skills", "scripts"]
SUFFIXES = {".md", ".mdx", ".py", ".sh", ".ps1", ".cmd"}

# A fetch-and-run command: curl/wget piped to a shell, or irm/iwr run by iex.
_FETCH_URL = re.compile(
    r"(?:curl|wget)\b[^\n|]*?(https?://[^\s|\"')]+)[^\n|]*\|\s*(?:ba|z)?sh\b"
    r"|\b(?:irm|iwr|Invoke-RestMethod|Invoke-WebRequest)\s+(?:-\S+\s+)*(https?://[^\s\"')]+)"
)


def _files():
    for entry in SEARCHED:
        path = ROOT / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file() and f.suffix in SUFFIXES and "node_modules" not in f.parts:
                    yield f


def test_fetch_and_run_commands_point_at_a_script():
    bad = []
    for f in _files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in _FETCH_URL.finditer(line):
                url = (m.group(1) or m.group(2)).rstrip(".,;")
                if re.fullmatch(r"https://github\.com/[^/]+/[^/]+/?", url):
                    bad.append(f"{f.relative_to(ROOT)}:{lineno}: {url}")
    assert not bad, "install command fetches a repo page, not a script:\n" + "\n".join(bad)
