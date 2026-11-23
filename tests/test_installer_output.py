"""What the installers print must render on the terminal they run in."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"

_OUTPUT_FUNCTIONS = ("rule", "print_banner", "log_info", "log_success", "log_warn",
                     "log_error", "print_row", "print_success")


def test_install_ps1_is_pure_ascii():
    # Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page;
    # a UTF-8 byte sequence can decode to a smart quote and break parsing.
    # Glyphs are built from [char] codes instead.
    bad = [f"{n}: {line.strip()[:80]}"
           for n, line in enumerate(INSTALL_PS1.read_text(encoding="utf-8").splitlines(), 1)
           if not line.isascii()]
    assert not bad, "non-ASCII in install.ps1:\n" + "\n".join(bad)


def _install_sh_output(env_overrides):
    """Run install.sh's output helpers (not the install) and return what they print."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    setup = re.search(r"^# Colors \(off.*?^unset _janus_utf8$", text, re.S | re.M)
    assert setup, "colour/glyph setup block not found in install.sh"
    parts = [setup.group(0)]
    for name in _OUTPUT_FUNCTIONS:
        fn = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.S | re.M)
        assert fn, f"{name}() not found in install.sh"
        parts.append(fn.group(0))
    parts.append(
        'print_banner; log_info "info"; log_success "ok"; log_warn "warn"; log_error "err"\n'
        "JANUS_HOME=/h; INSTALL_DIR=/h/janus-agent; DISTRO=linux; ROOT_FHS_LAYOUT=false\n"
        "HAS_NODE=false; HAS_RIPGREP=false; SHELL=/bin/zsh\n"
        "print_success\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
    env.update(env_overrides)
    proc = subprocess.run(["bash", "-c", "\n".join(parts)], capture_output=True,
                          text=True, encoding="utf-8", env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestInstallShOutput:
    def test_ascii_only_without_a_utf8_locale(self):
        out = _install_sh_output({"LANG": "C", "TERM": "xterm"})

        assert out.isascii(), [line for line in out.splitlines() if not line.isascii()]
        assert "Janus is installed" in out

    def test_linux_text_console_gets_ascii(self):
        out = _install_sh_output({"LANG": "en_US.UTF-8", "TERM": "linux"})

        assert out.isascii()

    def test_no_colour_codes_when_not_a_terminal(self):
        out = _install_sh_output({"LANG": "en_US.UTF-8", "TERM": "xterm"})

        assert "\033[" not in out

    def test_rows_line_up(self):
        out = _install_sh_output({"LANG": "en_US.UTF-8", "TERM": "xterm"})
        rows = [line for line in out.splitlines() if line.startswith("    janus")]

        assert len(rows) >= 3
        columns = {len(re.match(r"\s*janus[\w ]*?\s{2,}", r).group(0)) for r in rows}
        assert len(columns) == 1, rows
