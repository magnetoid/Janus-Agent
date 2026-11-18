"""The classic Windows console: ANSI colour codes and glyphs its font lacks.

conhost (the window Windows PowerShell 5.1 and cmd.exe open on Windows 10)
prints escape codes literally (``←[33m``) unless the program turns on
virtual-terminal processing, and it has no font fallback, so a check mark or
an emoji its font lacks shows as an empty box. Both made ``janus setup``,
which the Windows installer runs, look broken there.
"""
import io
import sys

import pytest


@pytest.fixture
def stdio(monkeypatch):
    sys.modules.pop("janus_cli.stdio", None)
    import janus_cli.stdio as module
    for var in ("WT_SESSION", "TERM_PROGRAM", "WEZTERM_PANE", "ALACRITTY_WINDOW_ID",
                "TERM", "ConEmuANSI", "JANUS_ASCII_GLYPHS"):
        monkeypatch.delenv(var, raising=False)
    yield module
    sys.modules.pop("janus_cli.stdio", None)


class _Tty(io.StringIO):
    def isatty(self):
        return True


# ---------------------------------------------------------------------------
# Glyphs
# ---------------------------------------------------------------------------

class TestSafeGlyphs:
    @pytest.mark.parametrize("fancy,plain", [
        ("✓ Saved", "+ Saved"),
        ("✗ Failed", "x Failed"),
        ("⚠ Careful", "! Careful"),
        ("◆ Provider", "* Provider"),
    ])
    def test_status_glyphs_become_ascii(self, stdio, fancy, plain):
        assert stdio.to_console_safe_glyphs(fancy) == plain

    def test_emoji_become_a_single_ascii_mark(self, stdio):
        out = stdio.to_console_safe_glyphs("📁 Files  ⚡ Fast  ⚠️ Warn")

        assert out == "* Files  * Fast  ! Warn"

    def test_text_the_console_font_has_is_left_alone(self, stdio):
        text = "Šta je ovo? Ćao, đače — žurim… ─│┌┐└┘ → [OK] Привет"

        assert stdio.to_console_safe_glyphs(text) == text


class TestWhenToUseAsciiGlyphs:
    def test_classic_console_on_windows(self, stdio, monkeypatch):
        monkeypatch.setattr(stdio, "is_windows", lambda: True)

        assert stdio.wants_ascii_glyphs(_Tty()) is True

    @pytest.mark.parametrize("var,value", [
        ("WT_SESSION", "1b6f"),            # Windows Terminal
        ("TERM_PROGRAM", "vscode"),
        ("WEZTERM_PANE", "0"),
        ("TERM", "xterm-256color"),        # Git Bash / mintty
        ("ConEmuANSI", "ON"),
    ])
    def test_terminals_with_font_fallback(self, stdio, monkeypatch, var, value):
        monkeypatch.setattr(stdio, "is_windows", lambda: True)
        monkeypatch.setenv(var, value)

        assert stdio.wants_ascii_glyphs(_Tty()) is False

    def test_not_a_terminal(self, stdio, monkeypatch):
        monkeypatch.setattr(stdio, "is_windows", lambda: True)

        assert stdio.wants_ascii_glyphs(io.StringIO()) is False

    def test_never_off_windows_unless_asked(self, stdio, monkeypatch):
        assert stdio.wants_ascii_glyphs(_Tty()) is False
        monkeypatch.setenv("JANUS_ASCII_GLYPHS", "1")
        assert stdio.wants_ascii_glyphs(_Tty()) is True

    def test_can_be_turned_off(self, stdio, monkeypatch):
        monkeypatch.setattr(stdio, "is_windows", lambda: True)
        monkeypatch.setenv("JANUS_ASCII_GLYPHS", "0")

        assert stdio.wants_ascii_glyphs(_Tty()) is False


class TestAsciiGlyphStream:
    def test_writes_are_translated(self, stdio):
        inner = _Tty()
        stream = stdio.AsciiGlyphStream(inner)

        written = stream.write("✓ done\n")
        stream.writelines(["⚠ a\n", "b\n"])

        assert inner.getvalue() == "+ done\n! a\nb\n"
        assert written == len("✓ done\n")

    def test_behaves_like_the_stream_it_wraps(self, stdio):
        inner = _Tty()
        stream = stdio.AsciiGlyphStream(inner)

        assert stream.isatty() is True
        stream.flush()
        assert stdio.AsciiGlyphStream(stream)._stream is inner  # never double-wrapped

    def test_configure_sets_up_the_classic_console(self, stdio, monkeypatch):
        monkeypatch.setattr(stdio, "is_windows", lambda: True)
        monkeypatch.setattr(stdio, "_reconfigure_stream", lambda *a, **kw: None)
        monkeypatch.setattr(stdio, "_flip_console_code_page_to_utf8", lambda: None)
        monkeypatch.setattr(stdio, "_enable_vt_processing", lambda: False)
        monkeypatch.setattr(stdio, "_default_windows_editor", lambda: "")
        monkeypatch.setattr(stdio, "_augment_path_with_known_tools", lambda: None)
        out, err = _Tty(), _Tty()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        stdio.configure_windows_stdio()
        print("✓ ok")

        assert out.getvalue() == "+ ok\n"
        assert isinstance(sys.stderr, stdio.AsciiGlyphStream)
        assert stdio.ansi_supported() is False  # the console refused escape codes


# ---------------------------------------------------------------------------
# ANSI colour codes
# ---------------------------------------------------------------------------

class _FakeKernel32:
    def __init__(self, mode=0, set_ok=True, console=True):
        self.mode, self.set_ok, self.console = mode, set_ok, console
        self.set_calls = []

    def GetStdHandle(self, which):
        return which

    def GetConsoleMode(self, handle, mode_ref):
        if not self.console:
            return 0
        mode_ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_calls.append((handle, mode))
        return 1 if self.set_ok else 0


class TestVirtualTerminal:
    def _use(self, stdio, monkeypatch, k32):
        monkeypatch.setattr(stdio, "_kernel32", lambda: k32)

    def test_turns_on_escape_processing_for_stdout_and_stderr(self, stdio, monkeypatch):
        k32 = _FakeKernel32(mode=0x0003)
        self._use(stdio, monkeypatch, k32)

        assert stdio._enable_vt_processing() is True
        assert {h for h, _ in k32.set_calls} == {-11, -12}
        assert all(m == 0x0003 | 0x0004 for _, m in k32.set_calls)

    def test_old_console_turns_colour_off(self, stdio, monkeypatch):
        self._use(stdio, monkeypatch, _FakeKernel32(set_ok=False))

        assert stdio._enable_vt_processing() is False

    def test_redirected_output_needs_nothing(self, stdio, monkeypatch):
        k32 = _FakeKernel32(console=False)
        self._use(stdio, monkeypatch, k32)

        assert stdio._enable_vt_processing() is True
        assert k32.set_calls == []

    def test_cli_colours_follow_the_console(self, stdio, monkeypatch):
        from janus_cli import colors

        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(sys, "stdout", _Tty())
        monkeypatch.setattr(stdio, "_ANSI_OK", False)
        assert colors.should_use_color() is False
        monkeypatch.setattr(stdio, "_ANSI_OK", True)
        assert colors.should_use_color() is True
