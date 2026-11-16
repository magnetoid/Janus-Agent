from types import SimpleNamespace

from janus_cli.status import show_status


def test_show_status_includes_tavily_key(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-1234567890abcdef")

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Tavily" in output
    assert "tvly...cdef" in output


def test_show_status_termux_gateway_section_skips_systemctl(monkeypatch, capsys, tmp_path):
    from janus_cli import status as status_mod
    import janus_cli.auth as auth_mod
    import janus_cli.gateway as gateway_mod

    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_janus_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)

    def _unexpected_systemctl(*args, **kwargs):
        raise AssertionError("systemctl should not be called in the Termux status view")

    monkeypatch.setattr(status_mod.subprocess, "run", _unexpected_systemctl)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Manager:      Termux / manual process" in output
    assert "Start with:   janus gateway" in output
    assert "systemd (user)" not in output


# ---------------------------------------------------------------------------
# Helpers shared by xAI OAuth status tests
# ---------------------------------------------------------------------------

def _base_xai_mocks(monkeypatch, tmp_path):
    """Set up the minimal environment for show_status, returning status_mod."""
    from janus_cli import status as status_mod
    import janus_cli.auth as auth_mod
    import janus_cli.gateway as gateway_mod

    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_janus_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_qwen_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_minimax_oauth_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)
    return status_mod


class TestShowStatusXaiOAuth:
    """xAI OAuth row in janus status."""

    # ------------------------------------------------------------------
    # Logged-in branch
    # ------------------------------------------------------------------

    def test_logged_in_shows_check_mark_and_label(self, monkeypatch, capsys, tmp_path):
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True, "auth_store": "/a/auth.json"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "xAI OAuth" in out
        # The logged-in label must appear; the "not logged in" label must not
        assert "✓" in out or "logged in" in out
        assert "not logged in" not in out.split("xAI OAuth", 1)[1].split("\n")[0]

    def test_logged_in_shows_auth_store(self, monkeypatch, capsys, tmp_path):
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True, "auth_store": "/home/u/.janus/auth.json"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "Auth file:  /home/u/.janus/auth.json" in out

    def test_logged_in_shows_last_refresh(self, monkeypatch, capsys, tmp_path):
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {
                                "logged_in": True,
                                "auth_store": "/a/auth.json",
                                "last_refresh": "2026-05-17T10:00:00+00:00",
                            },
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "Refreshed:" in out

    def test_logged_in_does_not_show_error_line(self, monkeypatch, capsys, tmp_path):
        """Error field must be suppressed when logged_in is True."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {
                                "logged_in": True,
                                "auth_store": "/a/auth.json",
                                "error": "stale-error-must-not-appear",
                            },
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1]
        assert "stale-error-must-not-appear" not in xai_section

    def test_no_auth_store_line_when_field_absent(self, monkeypatch, capsys, tmp_path):
        """Auth file line must not appear when auth_store is missing."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1].split("◆", 1)[0]
        assert "Auth file:" not in xai_section

    def test_no_refreshed_line_when_last_refresh_absent(self, monkeypatch, capsys, tmp_path):
        """Refreshed line must not appear when last_refresh is not present."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": True, "auth_store": "/a/auth.json"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1].split("◆", 1)[0]
        assert "Refreshed:" not in xai_section

    # ------------------------------------------------------------------
    # Not-logged-in branch
    # ------------------------------------------------------------------

    def test_not_logged_in_shows_login_command(self, monkeypatch, capsys, tmp_path):
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": False, "error": "no credentials"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "not logged in (run: janus auth add xai-oauth)" in out

    def test_not_logged_in_shows_error(self, monkeypatch, capsys, tmp_path):
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": False, "error": "Token has expired"},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "Error:      Token has expired" in out

    def test_not_logged_in_omits_error_line_when_error_absent(self, monkeypatch, capsys, tmp_path):
        """No Error: line when not logged in but error key is missing."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: {"logged_in": False},
                            raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        xai_section = out.split("xAI OAuth", 1)[1].split("◆", 1)[0]
        assert "Error:" not in xai_section

    # ------------------------------------------------------------------
    # Resilience: import failure and runtime exception
    # ------------------------------------------------------------------

    def test_import_failure_does_not_crash_show_status(self, monkeypatch, capsys, tmp_path):
        """show_status must complete even when get_xai_oauth_auth_status cannot be imported."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.delattr(auth_mod, "get_xai_oauth_auth_status", raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "◆ Auth Providers" in out

    def test_import_failure_does_not_break_other_oauth_providers(self, monkeypatch, capsys, tmp_path):
        """Codex/MiniMax rows must still appear when xAI import fails."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_codex_auth_status",
                            lambda: {"logged_in": True}, raising=False)
        monkeypatch.delattr(auth_mod, "get_xai_oauth_auth_status", raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "OpenAI Codex" in out
        assert "MiniMax OAuth" in out

    def test_status_function_exception_does_not_crash(self, monkeypatch, capsys, tmp_path):
        """show_status must not propagate an exception raised by get_xai_oauth_auth_status."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)

        def _raises():
            raise RuntimeError("backend unreachable")

        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status", _raises, raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "◆ Auth Providers" in out

    def test_status_function_returns_none_does_not_crash(self, monkeypatch, capsys, tmp_path):
        """get_xai_oauth_auth_status returning None must be handled gracefully."""
        import janus_cli.auth as auth_mod
        status_mod = _base_xai_mocks(monkeypatch, tmp_path)
        monkeypatch.setattr(auth_mod, "get_xai_oauth_auth_status",
                            lambda: None, raising=False)

        status_mod.show_status(SimpleNamespace(all=False, deep=False))
        out = capsys.readouterr().out

        assert "xAI OAuth" in out
        assert "not logged in (run: janus auth add xai-oauth)" in out
