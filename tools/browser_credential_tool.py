"""browser_fill_credential: sign in with a saved login, password unseen.

The model asks for a login to be filled on the page that is open; it never
chooses the site and never sees the password:

1. One page-side eval marks the page's single visible
   ``<input type=password>``, so the password can only land in a real
   password field (never a comment box or a search field the model points
   at). The site is read separately as the bare ``location.href``, returned
   by value: ``location`` and its ``href`` are unforgeable, and nothing in
   that read runs page code (no ``JSON.stringify`` or ``String`` a page could
   replace), so a page cannot pass itself off as another site. What the
   marking eval reports about the page is never used to decide the site.
2. The approval gate asks the user, per site
   (``tools.approval.check_credential_fill_guard``).
3. The field is marked and the URL read again right before the fill; if
   the page moved to another host while the user was deciding, nothing is
   filled.
4. The login comes from 1Password or Bitwarden
   (``agent.secret_sources.logins``) and is typed straight into the page.
   The result names the site and the saved item, nothing else, and every
   later tool result is scrubbed of the password.

Off by default (``browser.credential_fill.enabled``), and hidden from the
model until it is turned on.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "source": "",
    "vault": "",
    "allowed_sites": [],
    "allow_cloud_browser": False,
}

# Finds the visible password inputs, clears any earlier mark, and marks the
# field when there is exactly one. Runs in the page's context, so a hostile
# page can lie in its reply: only the field count is used from it, never a URL.
_MARK_JS = """(() => {
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
      && s.display !== 'none' && !el.disabled && !el.readOnly;
  };
  document.querySelectorAll('[data-janus-cred]').forEach((el) => el.removeAttribute('data-janus-cred'));
  const fields = Array.from(document.querySelectorAll('input[type="password"]')).filter(shown);
  if (fields.length === 1) fields[0].setAttribute('data-janus-cred', '%s');
  return fields.length;
})()"""

_UNMARK_JS = ("document.querySelectorAll('[data-janus-cred]')"
              ".forEach((el) => el.removeAttribute('data-janus-cred')); true")


def credential_fill_config() -> Dict[str, Any]:
    """``browser.credential_fill`` merged over the defaults. Never raises."""
    try:
        from janus_cli.config import load_config
        section = ((load_config().get("browser") or {}).get("credential_fill")) or {}
    except Exception:
        section = {}
    return {**_DEFAULTS, **(section if isinstance(section, dict) else {})}


def check_credential_fill_requirements() -> bool:
    cfg = credential_fill_config()
    if cfg.get("enabled") is not True:
        return False
    from tools import browser_tool
    return bool(browser_tool.check_browser_requirements())


def _fail(message: str) -> str:
    from agent.secret_sources.logins import scrub_filled_secrets
    return json.dumps({"success": False, "error": scrub_filled_secrets(message)},
                      ensure_ascii=False)


def _mark_password_field(bt, task_id: Optional[str]):
    """Mark the password field; return ``(count, token)`` or an error string."""
    token = uuid.uuid4().hex
    raw = bt._browser_eval(_MARK_JS % token, task_id)
    try:
        reply = json.loads(raw)
    except (TypeError, ValueError):
        return "could not read the page (unexpected browser reply)"
    if not reply.get("success"):
        return f"could not read the page: {reply.get('error') or 'eval failed'}"
    result = reply.get("result")
    if isinstance(result, bool) or not isinstance(result, (int, float, str)):
        return "could not read the page (unexpected browser reply)"
    try:
        return int(result), token
    except (TypeError, ValueError):
        return "could not read the page (unexpected browser reply)"


def _page_url(bt, session: str):
    """``(url, "")`` as the browser reports it, or ``("", error)``.

    The expression is the bare ``location.href`` (unforgeable getters) and
    the browser serializes the primitive itself, so no page-defined
    function takes part in producing the value.
    """
    res = bt._run_browser_command(session, "eval", ["location.href"])
    if not res.get("success"):
        return "", f"could not read the page URL: {res.get('error') or 'eval failed'}"
    value = (res.get("data") or {}).get("result")
    if isinstance(value, str):
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                pass
    if not isinstance(value, str) or not value:
        return "", "could not read the page URL (unexpected browser reply)"
    return value, ""


def _check_page(bt, task_id: Optional[str], session: str):
    """Mark the field, then read the URL: ``(href, host, token)`` or an error string."""
    from agent.secret_sources import logins

    marked = _mark_password_field(bt, task_id)
    if isinstance(marked, str):
        return marked
    count, token = marked
    href, error = _page_url(bt, session)
    if error:
        return error
    try:
        host = logins.page_host(href)
    except logins.LoginLookupError as exc:
        return str(exc)
    problem = _field_problem(count)
    if problem:
        return problem
    return href, host, token


def _field_problem(count: int) -> Optional[str]:
    if count == 0:
        return ("there is no visible password field on this page. For a two-step "
                "sign-in, fill the username with browser_type, continue, and call "
                "browser_fill_credential again on the password step")
    if count > 1:
        return (f"this page has {count} visible password fields (sign-up or "
                "change-password form?); logins are filled only where there is one")
    return None


def browser_fill_credential(username_ref: str = "", username: str = "",
                            task_id: Optional[str] = None) -> str:
    from agent.secret_sources import logins
    from tools import approval
    from tools import browser_tool as bt

    cfg = credential_fill_config()
    if cfg.get("enabled") is not True:
        return _fail("filling saved logins is off; the user can turn it on with "
                     "browser.credential_fill.enabled in config.yaml")
    if bt._is_camofox_mode():
        return _fail("filling saved logins is not supported with the Camofox browser")
    if bt._using_lightpanda_engine():
        return _fail("filling saved logins needs the Chrome engine (browser.engine)")
    if bt._get_cloud_provider() is not None and cfg.get("allow_cloud_browser") is not True:
        return _fail("the browser runs on a cloud provider; sending a saved password there "
                     "needs browser.credential_fill.allow_cloud_browser: true")

    session = bt._last_session_key(task_id or "default")
    page = _check_page(bt, task_id, session)
    if isinstance(page, str):
        return _fail(page)
    _href, host, _token = page

    guard = approval.check_credential_fill_guard(host, allowed_sites=cfg.get("allowed_sites") or ())
    if not guard.get("approved"):
        return _fail(guard.get("message") or f"filling a login on {host} was not approved")

    # Check the page again: it may have moved while the user was deciding.
    page = _check_page(bt, task_id, session)
    if isinstance(page, str):
        return _fail(page)
    href, now, token = page
    if now != host:
        return _fail(f"the page changed from {host} to {now} before the login was filled; "
                     "nothing was filled")

    try:
        login = logins.lookup_login(href, source=str(cfg.get("source") or ""),
                                    username=username or "", vault=str(cfg.get("vault") or ""))
    except logins.LoginLookupError as exc:
        return _fail(str(exc))

    logins.remember_filled_secret(login.password)
    filled = []
    try:
        if username_ref:
            ref = username_ref if username_ref.startswith("@") else f"@{username_ref}"
            res = bt._run_browser_command(session, "fill", [ref, login.username])
            if not res.get("success"):
                return _fail(f"could not fill the username into {ref}: {res.get('error') or 'fill failed'}")
            filled.append("username")
        res = bt._run_browser_command(session, "fill", [f'[data-janus-cred="{token}"]', login.password])
        if not res.get("success"):
            return _fail(f"could not fill the password field: {res.get('error') or 'fill failed'}")
        filled.append("password")
    finally:
        try:
            bt._browser_eval(_UNMARK_JS, task_id)
        except Exception:
            logger.debug("credential fill: could not clear the field mark", exc_info=True)

    # Audit line: the site only, nothing about the login itself.
    logger.info("credential fill: saved login filled on %s", host)
    return json.dumps({
        "success": True,
        "site": host,
        "item": login.title,
        "filled": filled,
        "note": ("The saved login was typed into the page; its values are not shown. "
                 "Submit the form to sign in."),
    }, ensure_ascii=False)


BROWSER_FILL_CREDENTIAL_SCHEMA = {
    "name": "browser_fill_credential",
    "description": (
        "Sign in on the page that is open with a login saved in the user's password "
        "manager. Fills the page's password field (and the username field you name) "
        "without you ever seeing the values. The site is the page's own URL; the user "
        "approves each site. Use it instead of asking the user for a password. Then "
        "submit the form with browser_click or browser_press."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "username_ref": {
                "type": "string",
                "description": "Ref of the username/email field from browser_snapshot (e.g. '@e3'). "
                               "Omit when the page asks only for the password.",
            },
            "username": {
                "type": "string",
                "description": "Which saved account to use when several match the site.",
            },
        },
        "required": [],
    },
}


registry.register(
    name="browser_fill_credential",
    toolset="browser",
    schema=BROWSER_FILL_CREDENTIAL_SCHEMA,
    handler=lambda args, **kw: browser_fill_credential(
        username_ref=args.get("username_ref", "") or "",
        username=args.get("username", "") or "",
        task_id=kw.get("task_id"),
    ),
    check_fn=check_credential_fill_requirements,
    emoji="🔑",
)
