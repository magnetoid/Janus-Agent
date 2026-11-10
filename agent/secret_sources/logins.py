"""Site logins from a password manager, for ``browser_fill_credential``.

Two sources, both through their official CLIs so Janus never stores a
password itself:

* ``1password`` — the ``op`` CLI (service-account token in
  ``OP_SERVICE_ACCOUNT_TOKEN``, or the desktop-app integration).
* ``bitwarden`` — the ``bw`` CLI with an unlocked session in ``BW_SESSION``.
  (Bitwarden *Secrets Manager*, ``bws``, holds API keys, not site logins;
  see ``bitwarden.py``.)

A login is offered only for the page it belongs to: the page must be HTTPS
(plain HTTP only on this machine), and its host must be the saved site's
host or a subdomain of it. The page URL is read from the browser by the
caller, never taken from the model.

Passwords handed to the browser are remembered in memory for the life of
the process so later tool output can be scrubbed of them
(:func:`scrub_filled_secrets`): once a password sits in a page's DOM, an
eval or a console read could otherwise carry it back to the model.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

REDACTED = "[credential redacted]"
SOURCES = ("1password", "bitwarden")

_RUN_TIMEOUT = 30

Runner = Callable[[List[str], dict], Tuple[int, str, str]]


class LoginLookupError(RuntimeError):
    """A login could not be offered. The message is safe to show the model."""


@dataclass(frozen=True)
class Login:
    title: str
    username: str
    password: str = field(repr=False)


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _norm(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def page_host(url: str) -> str:
    """The host a login may be filled on, or :class:`LoginLookupError`."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        parts = None
    host = _norm(parts.hostname or "") if parts else ""
    scheme = parts.scheme.lower() if parts else ""
    if not host or scheme not in {"https", "http"}:
        raise LoginLookupError(
            f"this page ({(url or 'no URL')[:80]}) is not a web page a login can be filled on")
    if scheme == "http" and not _is_loopback(host):
        raise LoginLookupError(
            f"{host} is served over plain HTTP; logins are filled only on HTTPS pages")
    return host


def login_matches_host(item_url: str, host: str) -> bool:
    """Whether a login saved for ``item_url`` belongs on a page at ``host``."""
    item_url = (item_url or "").strip()
    if not item_url:
        return False
    if "://" not in item_url:
        item_url = "https://" + item_url
    try:
        parts = urlsplit(item_url)
        port = parts.port
    except ValueError:
        return False
    saved = _norm(parts.hostname or "")
    if not saved:
        return False
    if port not in (None, 80, 443):
        return False
    host = _norm(host)
    return host == saved or host.endswith("." + saved)


# ---------------------------------------------------------------------------
# CLIs
# ---------------------------------------------------------------------------

def _default_runner(cmd: List[str], env: dict) -> Tuple[int, str, str]:
    binary = shutil.which(cmd[0])
    if binary is None:
        return 127, "", f"{cmd[0]} not found on PATH"
    try:
        proc = subprocess.run([binary, *cmd[1:]], capture_output=True, text=True,
                              env=env, timeout=_RUN_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} timed out after {_RUN_TIMEOUT}s"
    except OSError as exc:
        return 126, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _run_json(run: Runner, cmd: List[str], label: str):
    env = {**os.environ, "NO_COLOR": "1"}
    rc, out, err = run(cmd, env)
    if rc != 0:
        # The CLIs' errors name the problem (not signed in, locked, missing);
        # they never contain item contents.
        text = (err or out or "").strip()
        detail = text.splitlines()[0][:200] if text else f"exit code {rc}"
        raise LoginLookupError(f"{label} CLI failed: {detail}")
    try:
        return json.loads(out or "null")
    except ValueError:
        raise LoginLookupError(f"{label} CLI returned output that is not JSON") from None


def _onepassword_candidates(run: Runner, host: str, vault: str) -> List[Tuple[str, str]]:
    cmd = ["op", "item", "list", "--categories", "Login", "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    items = _run_json(run, cmd, "1Password") or []
    found = []
    for item in items if isinstance(items, list) else []:
        urls = [u.get("href", "") for u in item.get("urls") or [] if isinstance(u, dict)]
        if any(login_matches_host(u, host) for u in urls):
            found.append((str(item.get("id") or ""), str(item.get("title") or "login")))
    return found


def _onepassword_get(run: Runner, item_id: str, title: str, vault: str) -> Login:
    cmd = ["op", "item", "get", item_id, "--reveal", "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    item = _run_json(run, cmd, "1Password") or {}
    username = password = ""
    for f in item.get("fields") or []:
        purpose = str(f.get("purpose") or "").upper()
        if purpose == "USERNAME" and not username:
            username = str(f.get("value") or "")
        elif purpose == "PASSWORD" and not password:
            password = str(f.get("value") or "")
    if not password:
        raise LoginLookupError(f"the 1Password item '{title}' has no password field")
    return Login(title=title, username=username, password=password)


def _lookup_onepassword(run: Runner, host: str, username: str, vault: str) -> Login:
    candidates = _onepassword_candidates(run, host, vault)
    if not candidates:
        raise LoginLookupError(f"no saved login for {host} in 1Password")
    if len(candidates) == 1:
        login = _onepassword_get(run, *candidates[0], vault)
        if username and login.username != username:
            raise LoginLookupError(f"no saved login for {host} with that username in 1Password")
        return login
    if not username:
        raise _ambiguous(host, [t for _, t in candidates])
    # Usernames are only in the full item; fetch each until one matches.
    for item_id, title in candidates:
        login = _onepassword_get(run, item_id, title, vault)
        if login.username == username:
            return login
    raise LoginLookupError(f"no saved login for {host} with that username")


def _lookup_bitwarden(run: Runner, page_url: str, host: str, username: str) -> Login:
    if not os.environ.get("BW_SESSION"):
        raise LoginLookupError(
            "the Bitwarden vault is locked: run `bw unlock` and export BW_SESSION "
            "in the environment Janus runs in")
    items = _run_json(run, ["bw", "list", "items", "--url", page_url], "Bitwarden") or []
    logins = []
    for item in items if isinstance(items, list) else []:
        login = item.get("login") or {}
        uris = [u.get("uri", "") for u in login.get("uris") or [] if isinstance(u, dict)]
        if login.get("password") and any(login_matches_host(u, host) for u in uris):
            logins.append(Login(title=str(item.get("name") or "login"),
                                username=str(login.get("username") or ""),
                                password=str(login["password"])))
    if username:
        logins = [x for x in logins if x.username == username]
    if not logins:
        suffix = " with that username" if username else ""
        raise LoginLookupError(f"no saved login for {host}{suffix} in Bitwarden")
    if len(logins) > 1:
        raise _ambiguous(host, [x.title for x in logins])
    return logins[0]


def _ambiguous(host: str, titles: Iterable[str]) -> LoginLookupError:
    titles = list(titles)
    return LoginLookupError(
        f"{len(titles)} saved logins match {host} ({', '.join(titles[:5])}); "
        "pass `username` to pick one")


def lookup_login(page_url: str, *, source: str, username: str = "", vault: str = "",
                 runner: Optional[Runner] = None) -> Login:
    """The one saved login for the page at ``page_url``.

    Raises :class:`LoginLookupError` (with a message safe to show the model)
    when the page is not eligible, nothing matches, several match and no
    ``username`` picks one, or the password manager CLI fails.
    """
    source = (source or "").strip().lower()
    if source not in SOURCES:
        raise LoginLookupError(
            f"browser.credential_fill.source must be one of {', '.join(SOURCES)}")
    host = page_host(page_url)
    run = runner or _default_runner
    if source == "1password":
        return _lookup_onepassword(run, host, username, vault)
    return _lookup_bitwarden(run, page_url, host, username)


# ---------------------------------------------------------------------------
# Scrubbing what was filled
# ---------------------------------------------------------------------------

_FILLED: set = set()
_FILLED_LOCK = threading.Lock()


def remember_filled_secret(value: str) -> None:
    """Mask ``value`` in every later tool result of this process."""
    if not value:
        return
    escaped = json.dumps(value)[1:-1]
    ascii_escaped = json.dumps(value, ensure_ascii=True)[1:-1]
    with _FILLED_LOCK:
        _FILLED.update({value, escaped, ascii_escaped})


def scrub_filled_secrets(text):
    """``text`` with every remembered secret replaced by :data:`REDACTED`."""
    if not _FILLED or not isinstance(text, str):
        return text
    with _FILLED_LOCK:
        secrets = sorted(_FILLED, key=len, reverse=True)
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _reset_filled_secrets_for_tests() -> None:
    with _FILLED_LOCK:
        _FILLED.clear()
