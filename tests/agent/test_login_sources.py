"""Site logins from a password manager, for browser_fill_credential."""
import json

import pytest

from agent.secret_sources import logins as L


@pytest.fixture(autouse=True)
def _clean_filled():
    L._reset_filled_values_for_tests()
    yield
    L._reset_filled_values_for_tests()


class TestPageHost:
    def test_https_page_gives_its_host(self):
        assert L.page_host("https://GitHub.com/login?return_to=/x") == "github.com"

    @pytest.mark.parametrize("url", ["http://github.com/login", "file:///tmp/x.html",
                                     "javascript:alert(1)", "about:blank", ""])
    def test_non_https_pages_are_refused(self, url):
        with pytest.raises(L.LoginLookupError):
            L.page_host(url)

    def test_plain_http_is_allowed_on_this_machine_only(self):
        assert L.page_host("http://localhost:3000/login") == "localhost"
        assert L.page_host("http://127.0.0.1:8080/") == "127.0.0.1"


class TestHostMatching:
    @pytest.mark.parametrize("item_url,host", [
        ("https://github.com", "github.com"),
        ("github.com", "github.com"),                   # saved without a scheme
        ("https://www.github.com/login", "github.com"),
        ("https://github.com", "gist.github.com"),       # subdomain of the saved site
    ])
    def test_matches(self, item_url, host):
        assert L.login_matches_host(item_url, host)

    @pytest.mark.parametrize("item_url,host", [
        ("https://github.com", "github.com.evil.io"),    # suffix trick
        ("https://github.com", "evilgithub.com"),
        ("https://accounts.google.com", "google.com"),   # parent of the saved site
        ("https://github.com:8443", "github.com"),       # saved with another port
        ("", "github.com"),
    ])
    def test_does_not_match(self, item_url, host):
        assert not L.login_matches_host(item_url, host)


def _runner(responses):
    """A fake CLI runner: maps the first two argv words to (rc, stdout, stderr)."""
    calls = []

    def run(cmd, env):
        calls.append(cmd)
        for prefix, reply in responses.items():
            if tuple(cmd[1:1 + len(prefix)]) == prefix:
                return reply
        raise AssertionError(f"unexpected command {cmd}")

    run.calls = calls
    return run


_OP_LIST = json.dumps([
    {"id": "gh1", "title": "GitHub", "urls": [{"href": "https://github.com", "primary": True}]},
    {"id": "ev1", "title": "Evil", "urls": [{"href": "https://github.com.evil.io"}]},
    {"id": "gl1", "title": "GitLab", "urls": [{"href": "https://gitlab.com"}]},
])
_OP_ITEM = json.dumps({"fields": [
    {"id": "username", "purpose": "USERNAME", "value": "octo"},
    {"id": "password", "purpose": "PASSWORD", "value": "hunter2-very-secret"},
    {"id": "notes", "purpose": "NOTES", "value": "ignored"},
]})


class TestOnePassword:
    def test_fetches_only_the_matching_item(self):
        run = _runner({("item", "list"): (0, _OP_LIST, ""), ("item", "get"): (0, _OP_ITEM, "")})

        login = L.lookup_login("https://github.com/login", source="1password", runner=run)

        assert (login.title, login.username, login.password) == ("GitHub", "octo", "hunter2-very-secret")
        gets = [c for c in run.calls if c[1:3] == ["item", "get"]]
        assert len(gets) == 1 and "gh1" in gets[0]

    def test_password_never_appears_in_repr(self):
        run = _runner({("item", "list"): (0, _OP_LIST, ""), ("item", "get"): (0, _OP_ITEM, "")})

        login = L.lookup_login("https://github.com/login", source="1password", runner=run)

        assert "hunter2" not in repr(login) and "hunter2" not in str(login)

    def test_no_match_says_so_without_revealing_anything(self):
        run = _runner({("item", "list"): (0, _OP_LIST, "")})

        with pytest.raises(L.LoginLookupError, match="no saved login"):
            L.lookup_login("https://example.org/", source="1password", runner=run)
        assert not [c for c in run.calls if c[1:3] == ["item", "get"]]

    def test_vault_is_passed_through(self):
        run = _runner({("item", "list"): (0, _OP_LIST, ""), ("item", "get"): (0, _OP_ITEM, "")})

        L.lookup_login("https://github.com/", source="1password", vault="Work", runner=run)

        assert all("--vault" in c and "Work" in c for c in run.calls)

    def test_a_single_match_with_another_username_is_refused(self):
        run = _runner({("item", "list"): (0, _OP_LIST, ""), ("item", "get"): (0, _OP_ITEM, "")})

        with pytest.raises(L.LoginLookupError, match="that username"):
            L.lookup_login("https://github.com/", source="1password", username="someone-else",
                           runner=run)

    def test_cli_failure_becomes_a_lookup_error(self):
        run = _runner({("item", "list"): (1, "", "[ERROR] not signed in")})

        with pytest.raises(L.LoginLookupError, match="1Password"):
            L.lookup_login("https://github.com/", source="1password", runner=run)


def _bw_items(*items):
    return json.dumps([
        {"name": name, "login": {"username": user, "password": pw, "uris": [{"uri": uri}]}}
        for name, user, pw, uri in items
    ])


class TestBitwarden:
    def test_matching_item_is_returned(self, monkeypatch):
        monkeypatch.setenv("BW_SESSION", "s")
        run = _runner({("list", "items"): (0, _bw_items(
            ("GitHub", "octo", "pw-1", "https://github.com"),
            ("Lookalike", "x", "pw-2", "https://github.com.evil.io"),
        ), "")})

        login = L.lookup_login("https://github.com/login", source="bitwarden", runner=run)

        assert (login.title, login.username, login.password) == ("GitHub", "octo", "pw-1")

    def test_locked_vault_explains_how_to_unlock(self, monkeypatch):
        monkeypatch.delenv("BW_SESSION", raising=False)
        run = _runner({})

        with pytest.raises(L.LoginLookupError, match="BW_SESSION"):
            L.lookup_login("https://github.com/", source="bitwarden", runner=run)
        assert run.calls == []


class TestSeveralLogins:
    def _run(self):
        return _runner({("list", "items"): (0, _bw_items(
            ("GitHub work", "octo-work", "pw-w", "https://github.com"),
            ("GitHub home", "octo", "pw-h", "https://github.com"),
        ), "")})

    def test_ambiguous_without_a_username(self, monkeypatch):
        monkeypatch.setenv("BW_SESSION", "s")

        with pytest.raises(L.LoginLookupError) as err:
            L.lookup_login("https://github.com/", source="bitwarden", runner=self._run())
        assert "2 saved logins" in str(err.value)
        assert "pw-" not in str(err.value)

    def test_username_picks_one(self, monkeypatch):
        monkeypatch.setenv("BW_SESSION", "s")

        login = L.lookup_login("https://github.com/", source="bitwarden", username="octo",
                               runner=self._run())

        assert login.password == "pw-h"


def test_unknown_source_is_refused():
    with pytest.raises(L.LoginLookupError, match="source"):
        L.lookup_login("https://github.com/", source="keepass", runner=_runner({}))


class TestScrub:
    def test_filled_secret_is_masked_in_later_output(self):
        L.remember_filled_value("hunter2-very-secret")

        out = L.redact_filled_values('{"result": "value=hunter2-very-secret"}')

        assert "hunter2" not in out
        assert L.REDACTED in out

    def test_json_escaped_form_is_masked_too(self):
        L.remember_filled_value('pa"ss\\word')

        out = L.redact_filled_values(json.dumps({"result": 'pa"ss\\word'}))

        assert "ss\\\\word" not in out and L.REDACTED in out

    def test_nothing_remembered_leaves_text_alone(self):
        assert L.redact_filled_values("plain") == "plain"
