"""browser_fill_credential: sign in from a password manager without the model
ever seeing the password."""
import json

import pytest

from agent.secret_sources import logins as L
from tools import approval as A
from tools import browser_credential_tool as T
from tools import browser_tool as bt

SECRET = "hunter2-very-secret"


@pytest.fixture(autouse=True)
def _clean():
    L._reset_filled_secrets_for_tests()
    with A._lock:
        A._permanent_approved.clear()
        A._session_approved.clear()
    yield
    L._reset_filled_secrets_for_tests()
    with A._lock:
        A._permanent_approved.clear()
        A._session_approved.clear()


class Page:
    """A fake browser page: answers the marking eval and records fills."""

    def __init__(self, monkeypatch, *, urls=("https://github.com/login",), fields=1,
                 fill_result=None):
        self.urls = list(urls)
        self.fields = fields
        self.evals = 0
        self.fills = []
        self.fill_result = fill_result or {"success": True, "data": {}}
        monkeypatch.setattr(bt, "_browser_eval", self._eval)
        monkeypatch.setattr(bt, "_run_browser_command", self._command)
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
        monkeypatch.setattr(bt, "_using_lightpanda_engine", lambda: False)

    def _eval(self, expression, task_id=None):
        href = self.urls[min(self.evals, len(self.urls) - 1)]
        self.evals += 1
        return json.dumps({"success": True, "result": {"href": href, "count": self.fields}})

    def _command(self, task_id, command, args=None, timeout=None, _engine_override=None):
        if command == "fill":
            self.fills.append(list(args))
            return self.fill_result
        return {"success": True, "data": {}}


@pytest.fixture
def enabled(monkeypatch):
    cfg = {"enabled": True, "source": "1password", "vault": "", "allowed_sites": [],
           "allow_cloud_browser": False}
    monkeypatch.setattr(T, "credential_fill_config", lambda: dict(cfg))
    return cfg


@pytest.fixture
def approve(monkeypatch):
    seen = []

    def guard(host, allowed_sites=()):
        seen.append(host)
        return {"approved": True, "message": None}

    monkeypatch.setattr(A, "check_credential_fill_guard", guard)
    return seen


@pytest.fixture
def vault(monkeypatch):
    calls = []

    def lookup(page_url, **kw):
        calls.append((page_url, kw))
        return L.Login(title="GitHub", username="octo", password=SECRET)

    monkeypatch.setattr(L, "lookup_login", lookup)
    return calls


def _call(**kw):
    return json.loads(T.browser_fill_credential(**kw))


def test_disabled_by_default_and_touches_nothing(monkeypatch, vault):
    page = Page(monkeypatch)
    monkeypatch.setattr(T, "credential_fill_config", lambda: {"enabled": False})

    res = _call(username_ref="@e1")

    assert res["success"] is False and "credential_fill" in res["error"]
    assert page.evals == 0 and vault == []


def test_fills_username_and_the_marked_password_field(monkeypatch, enabled, approve, vault):
    page = Page(monkeypatch)

    res = _call(username_ref="e1")

    assert res["success"] is True and res["site"] == "github.com"
    assert page.fills[0] == ["@e1", "octo"]
    selector, value = page.fills[1]
    assert value == SECRET and selector.startswith("[data-janus-cred=")
    assert approve == ["github.com"]


def test_result_never_carries_the_password(monkeypatch, enabled, approve, vault):
    Page(monkeypatch)

    raw = T.browser_fill_credential(username_ref="@e1")

    assert SECRET not in raw


def test_the_site_comes_from_the_page_not_the_model(monkeypatch, enabled, approve, vault):
    Page(monkeypatch, urls=("https://github.com/login",))

    _call()

    assert vault[0][0] == "https://github.com/login"


@pytest.mark.parametrize("url", ["http://github.com/login", "about:blank"])
def test_insecure_page_is_refused_before_asking(monkeypatch, enabled, approve, vault, url):
    page = Page(monkeypatch, urls=(url,))

    res = _call()

    assert res["success"] is False
    assert approve == [] and vault == [] and page.fills == []


@pytest.mark.parametrize("fields,word", [(0, "no visible password field"), (2, "2 visible password fields")])
def test_needs_exactly_one_password_field(monkeypatch, enabled, approve, vault, fields, word):
    page = Page(monkeypatch, fields=fields)

    res = _call()

    assert res["success"] is False and word in res["error"]
    assert vault == [] and page.fills == []


def test_denied_approval_reads_nothing_from_the_vault(monkeypatch, enabled, vault):
    page = Page(monkeypatch)
    monkeypatch.setattr(A, "check_credential_fill_guard",
                        lambda host, allowed_sites=(): {"approved": False, "message": "BLOCKED: denied"})

    res = _call()

    assert res["success"] is False and "BLOCKED" in res["error"]
    assert vault == [] and page.fills == []


def test_page_that_moved_during_approval_is_refused(monkeypatch, enabled, approve, vault):
    page = Page(monkeypatch, urls=("https://github.com/login", "https://evil.example/login"))

    res = _call()

    assert res["success"] is False and "changed" in res["error"]
    assert vault == [] and page.fills == []


def test_cloud_browser_is_refused_unless_allowed(monkeypatch, enabled, approve, vault):
    page = Page(monkeypatch)
    monkeypatch.setattr(bt, "_get_cloud_provider", lambda: object())

    assert _call()["success"] is False
    assert page.fills == []

    enabled["allow_cloud_browser"] = True
    assert _call()["success"] is True


def test_fill_error_is_scrubbed(monkeypatch, enabled, approve, vault):
    Page(monkeypatch, fill_result={"success": False, "error": f"could not fill value {SECRET}"})

    raw = T.browser_fill_credential()

    assert json.loads(raw)["success"] is False and SECRET not in raw


def test_later_tool_output_has_the_password_masked(monkeypatch, enabled, approve, vault):
    import model_tools

    Page(monkeypatch)
    _call()
    monkeypatch.setattr(model_tools.registry, "dispatch",
                        lambda name, args, **kw: json.dumps({"result": SECRET}))

    out = model_tools.handle_function_call("browser_console", {"expression": "x"}, task_id="t")

    assert SECRET not in out and L.REDACTED in out


def test_hidden_from_the_model_while_disabled(monkeypatch):
    monkeypatch.setattr(bt, "check_browser_requirements", lambda: True)
    monkeypatch.setattr(T, "credential_fill_config", lambda: {"enabled": False})
    assert T.check_credential_fill_requirements() is False

    monkeypatch.setattr(T, "credential_fill_config", lambda: {"enabled": True, "source": "1password"})
    assert T.check_credential_fill_requirements() is True


def test_listed_in_the_browser_toolset():
    from toolsets import TOOLSETS

    assert "browser_fill_credential" in TOOLSETS["browser"]["tools"]


# ---------------------------------------------------------------------------
# The approval gate
# ---------------------------------------------------------------------------

@pytest.fixture
def gw_session(monkeypatch):
    monkeypatch.setenv("JANUS_GATEWAY_SESSION", "1")
    for var in ("JANUS_INTERACTIVE", "JANUS_CRON_SESSION", "JANUS_EXEC_ASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    key = "cred-fill-test"
    token = A.set_current_session_key(key)
    with A._lock:
        A._gateway_queues.pop(key, None)
        A._gateway_notify_cbs.pop(key, None)
    try:
        yield key
    finally:
        A.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(key, None)
            A._gateway_notify_cbs.pop(key, None)


def _answer(session_key, choice, seen=None):
    def cb(data):
        if seen is not None:
            seen.append(data)
        with A._lock:
            entry = A._gateway_queues[session_key][-1]
            entry.result = choice
            entry.event.set()
    with A._lock:
        A._gateway_notify_cbs[session_key] = cb


def test_gate_asks_and_names_the_site(gw_session):
    seen = []
    _answer(gw_session, "once", seen)

    res = A.check_credential_fill_guard("github.com")

    assert res["approved"] is True
    assert "github.com" in seen[0]["description"]
    assert not A.is_approved(gw_session, "credential_fill:github.com")


def test_gate_session_approval_is_per_site(gw_session):
    _answer(gw_session, "session")
    A.check_credential_fill_guard("github.com")

    assert A.is_approved(gw_session, "credential_fill:github.com")
    assert not A.is_approved(gw_session, "credential_fill:gitlab.com")


def test_gate_denied(gw_session):
    _answer(gw_session, "deny")

    assert A.check_credential_fill_guard("github.com")["approved"] is False


def test_gate_is_not_bypassed_by_yolo_or_approvals_off(gw_session, monkeypatch):
    A.enable_session_yolo(gw_session)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "off")
    _answer(gw_session, "deny")
    try:
        assert A.check_credential_fill_guard("github.com")["approved"] is False
    finally:
        A.disable_session_yolo(gw_session)


def test_gate_pre_approved_site_skips_the_prompt(gw_session):
    asked = []
    _answer(gw_session, "deny", asked)

    res = A.check_credential_fill_guard("github.com", allowed_sites=["www.GitHub.com"])

    assert res["approved"] is True and asked == []


@pytest.mark.parametrize("env", ["JANUS_CRON_SESSION", None])
def test_gate_blocks_when_nobody_can_answer(monkeypatch, env):
    for var in ("JANUS_GATEWAY_SESSION", "JANUS_INTERACTIVE", "JANUS_EXEC_ASK", "JANUS_CRON_SESSION"):
        monkeypatch.delenv(var, raising=False)
    if env:
        monkeypatch.setenv(env, "1")
    monkeypatch.setattr(A, "_get_headless_approval_mode", lambda: "approve")

    assert A.check_credential_fill_guard("github.com")["approved"] is False


def test_gate_cli_prompts_through_the_cli_callback(monkeypatch):
    from tools import terminal_tool

    for var in ("JANUS_GATEWAY_SESSION", "JANUS_CRON_SESSION", "JANUS_EXEC_ASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JANUS_INTERACTIVE", "1")
    asked = []

    def cb(command, description, *, allow_permanent=True):
        asked.append(description)
        return "once"

    terminal_tool.set_approval_callback(cb)
    try:
        assert A.check_credential_fill_guard("github.com")["approved"] is True
    finally:
        terminal_tool.set_approval_callback(None)
    assert asked and "github.com" in asked[0]


# ---------------------------------------------------------------------------
# Security posture
# ---------------------------------------------------------------------------

def _posture(tmp_path, credential_fill, *, cron=False):
    from janus_cli import security_posture as sp

    if cron:
        (tmp_path / "cron").mkdir()
        (tmp_path / "cron" / "jobs.json").write_text("[]", encoding="utf-8")
    cfg = {"browser": {"credential_fill": credential_fill}}
    return {f.check_id: f for f in sp.collect(cfg, cfg, tmp_path, {})
            if f.check_id.startswith("browser.credential_fill")}


def test_posture_flags_passwords_sent_to_a_cloud_browser(tmp_path):
    found = _posture(tmp_path, {"enabled": True, "allow_cloud_browser": True})

    f = found["browser.credential_fill_cloud"]
    assert f.fixable and f.key == "browser.credential_fill.allow_cloud_browser" and f.safe_value is False


def test_posture_flags_unattended_prefill_only_with_an_unattended_surface(tmp_path):
    cfg = {"enabled": True, "allowed_sites": ["github.com"]}

    assert "browser.credential_fill_unattended" not in _posture(tmp_path, cfg)
    assert "browser.credential_fill_unattended" in _posture(tmp_path, cfg, cron=True)


def test_posture_is_quiet_while_the_feature_is_off(tmp_path):
    assert _posture(tmp_path, {"enabled": False, "allow_cloud_browser": True,
                               "allowed_sites": ["github.com"]}, cron=True) == {}
