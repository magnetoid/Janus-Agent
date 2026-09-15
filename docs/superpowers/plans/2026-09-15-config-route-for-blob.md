# Config route, per-run instructions and a restart hook — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Blob read and change Janus's configuration through the API server, send per-workspace instructions with a run, and have Janus restart itself gracefully after a change — release 0.17.0.

**Architecture:** Three additive changes. `agui_protocol.parse_run_input` reads `forwardedProps.instructions` and `_handle_agui` folds it into the ephemeral system prompt. `BasePlatformAdapter` gains `set_restart_handler`, the runner extracts its `/restart` decision into `_request_gateway_restart()` and wires it to every adapter. The API server adapter gains `GET`/`PUT /v1/config`, built on `janus_cli.config`'s own primitives (`load_config`, `read_raw_config`, `_set_nested`, `validate_config_structure`, `save_env_value`, `atomic_yaml_write`) and `janus_cli.auth.PROVIDER_REGISTRY`.

**Tech Stack:** Python 3.11, aiohttp, PyYAML, pytest through `scripts/run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-15-config-route-for-blob-design.md` — binding.

## Global Constraints

- Work on branch `blob-config-route`. The checkout carries five modified files that are **not this work's** (`janus_cli/dep_ensure.py`, `janus_cli/main.py`, `tests/janus_cli/test_verify_core_dependencies.py`, `tests/tools/test_browser_hardening.py`, `tests/tools/test_browser_homebrew_paths.py`). Never `git add -A` or `git commit -a`; stage the files you touched by name. Do not push.
- Tests run only through `scripts/run_tests.sh` (CI parity): `scripts/run_tests.sh tests/gateway/test_api_server_config.py`. Lint: `.venv/bin/ruff check .` (rule PLW1514: every `open()`/`read_text()`/`write_text()` passes `encoding="utf-8"`). Both green before every commit.
- Never hardcode `~/.janus`: use `get_janus_home()` / `get_config_path()` / `get_env_path()`. Tests get an isolated home from the autouse `_isolate_janus_home` fixture.
- No new dependencies. No change to any existing route's behaviour.
- A key's value appears in no response body and no log line, ever.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: `forwardedProps.instructions`

**Files:**
- Modify: `gateway/platforms/agui.py` (`RunInput` ~line 119, `parse_run_input` ~line 130)
- Modify: `gateway/platforms/api_server.py` (`_handle_agui`, the `ephemeral_system_prompt=run.context_prompt` keyword ~line 3637)
- Test: the existing test module for `agui.py` (`grep -rln "parse_run_input" tests/`), and `tests/gateway/test_api_server.py` or `test_api_server_runs.py` for the handler

**Interfaces:**
- Produces: `RunInput.instructions: Optional[str]`; `MAX_INSTRUCTIONS_CHARS = 4000`.

- [ ] **Step 1: Failing tests**

In the `agui.py` test module:

```python
def test_instructions_are_read_from_forwarded_props():
    run = parse_run_input({**MINIMAL_RUN, "forwardedProps": {"instructions": "  Be brief.  "}})
    assert run.instructions == "Be brief."


def test_instructions_that_are_not_a_string_are_ignored():
    run = parse_run_input({**MINIMAL_RUN, "forwardedProps": {"instructions": ["no"]}})
    assert run.instructions is None


def test_instructions_are_capped():
    run = parse_run_input({**MINIMAL_RUN, "forwardedProps": {"instructions": "x" * 5000}})
    assert len(run.instructions) == 4000
```

(`MINIMAL_RUN` is whatever the module already uses for a valid body; if it has none, build one from `parse_run_input`'s required keys.) Run: expect `AttributeError`/assertion failures.

- [ ] **Step 2: Implement**

`RunInput` gains `instructions: Optional[str] = None`. In `parse_run_input`, after the context prompt is derived:

```python
    forwarded = body.get("forwardedProps")
    instructions = None
    if isinstance(forwarded, dict):
        raw = forwarded.get("instructions")
        if isinstance(raw, str) and raw.strip():
            instructions = raw.strip()[:MAX_INSTRUCTIONS_CHARS]
```

In `_handle_agui`, replace `ephemeral_system_prompt=run.context_prompt` with `ephemeral_system_prompt=_ephemeral_prompt(run)` where, in `agui.py`:

```python
INSTRUCTIONS_HEADING = "Instructions from this workspace's admin:"


def ephemeral_prompt(run: RunInput) -> Optional[str]:
    """The per-run system prompt: the workspace's instructions, then the room's context."""
    parts = []
    if run.instructions:
        parts.append(f"{INSTRUCTIONS_HEADING}\n{run.instructions}")
    if run.context_prompt:
        parts.append(run.context_prompt)
    return "\n\n".join(parts) or None
```

and a test that `ephemeral_prompt` puts the instructions first and returns the context alone when there are none, plus one handler test (pattern: `test_api_server_runs.py`'s assertions on `_run_agent`'s kwargs) that the heading reaches `ephemeral_system_prompt`.

- [ ] **Step 3: Run, lint, commit**

`scripts/run_tests.sh <the two test files>`; `.venv/bin/ruff check .`. Commit: `feat(agui): honour forwardedProps.instructions as the run's leading system prompt`.

---

### Task 2: A restart the API server can ask for

**Files:**
- Modify: `gateway/platforms/base.py` (beside `set_message_handler` ~line 2156)
- Modify: `gateway/runner.py` (`_handle_restart_command` ~line 9114–9210; the adapter setup ~line 2774 and the second site ~line 4526)
- Test: `tests/gateway/test_gateway_shutdown.py` or a new `tests/gateway/test_restart_handler.py`

**Interfaces:**
- Produces: `BasePlatformAdapter.set_restart_handler(handler: Callable[[], bool]) -> None`, `BasePlatformAdapter._restart_handler: Optional[Callable[[], bool]]`; `GatewayRunner._request_gateway_restart() -> bool`.

- [ ] **Step 1: Extract**

In `runner.py`, move the decision at ~lines 9199–9204 into:

```python
    def _request_gateway_restart(self) -> bool:
        """Ask for the graceful restart the /restart command performs.

        Under systemd or in a container the process exits 75 after the drain and the
        service manager or supervisor starts it again; anywhere else it re-execs itself
        detached. Returns False when a restart was already under way.
        """
        under_service = bool(os.environ.get("INVOCATION_ID"))  # systemd sets this
        in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
        if under_service or in_container:
            return self.request_restart(detached=False, via_service=True)
        return self.request_restart(detached=True, via_service=False)
```

and call it from `_handle_restart_command` in place of the inlined branch — behaviour unchanged. In `base.py`:

```python
    def set_restart_handler(self, handler: Callable[[], bool]) -> None:
        """Let this adapter ask the gateway for a graceful restart.

        Set by the runner beside the message handler. An adapter running without a
        gateway — the standalone API server — has none, and says so instead of failing.
        """
        self._restart_handler = handler
```

with `self._restart_handler: Optional[Callable[[], bool]] = None` in `__init__`. In the runner, at both places `set_message_handler` is called on an adapter, add `adapter.set_restart_handler(self._request_gateway_restart)`.

- [ ] **Step 2: Tests**

`_request_gateway_restart` chooses `via_service=True` with `/.dockerenv` patched present (`monkeypatch.setattr(os.path, "exists", ...)` narrowly) and the detached path otherwise — assert on `request_restart`'s kwargs with a `MagicMock`. `_handle_restart_command` still triggers exactly one `request_restart` (extend the existing shutdown/restart test if one drives the command). A `BasePlatformAdapter` subclass used in tests has `_restart_handler is None` until set.

- [ ] **Step 3: Run, lint, commit**

Commit: `feat(gateway): let an adapter ask for the graceful restart /restart performs`.

---

### Task 3: `GET` and `PUT /v1/config`

**Files:**
- Modify: `gateway/platforms/api_server.py` (route registration ~line 4322 beside `/v1/toolsets`; new handlers beside `_handle_toolsets` ~line 1254; the capabilities `features` map may gain `"config": True`)
- Create: `gateway/platforms/api_config.py` — the pure parts: reading the view, validating and applying a change, provider key presence, the provider model list. The adapter's two handlers stay thin: auth, JSON in, call, JSON out.
- Modify: `janus_cli/config.py` only if no `remove_env_value` exists (add one beside `save_env_value`, same guards).
- Create: `tests/gateway/test_api_server_config.py`

**Interfaces:**
- Produces (in `api_config.py`): `read_view(*, version: str, restart_pending: bool, fetch_models=...) -> dict`; `class ConfigChangeError(Exception)` carrying `status` and `payload`; `apply_change(body: dict) -> tuple[dict, list[dict]]` returning `(applied, warnings)` or raising `ConfigChangeError`; `provider_models(provider_id: str, *, timeout: float = 10.0) -> tuple[Optional[list[str]], Optional[str]]`; `KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN)$")`; `MODEL_KEYS = ("default", "provider", "base_url")`; `AGENT_KEYS = ("max_turns", "reasoning_effort", "gateway_timeout", "personality")`.

- [ ] **Step 1: Failing tests first**

Write `tests/gateway/test_api_server_config.py` from the spec's Tests list, building the app the way `test_api_server.py`'s `_make_adapter`/`_create_app` do, plus:

```python
    app.router.add_get("/v1/config", adapter._handle_get_config)
    app.router.add_put("/v1/config", adapter._handle_put_config)
```

Patch the provider fetch with `monkeypatch.setattr(api_config, "provider_models", fake)` for the `models` cases, and `monkeypatch.setattr(api_config, "is_managed", lambda: True)` (import it into `api_config` by name so the patch reaches it) for the 409 case. Run: every test fails on the missing handlers.

- [ ] **Step 2: `api_config.py`**

Reading: `load_config()` for `model`/`agent`, `read_raw_config()` is *not* used for `raw` — read the file text with `get_config_path().read_text(encoding="utf-8")` when it exists, else `""`. Providers: iterate `PROVIDER_REGISTRY.values()` with `auth_type == "api_key"`; `env = api_key_env_vars[0]`; presence = `os.environ.get(env) or get_env_value(env)` (find the existing reader of `.env` values — `get_env_value` at ~line 6312 — and use it); `tail = value[-4:]`. Toolsets: reuse what `_handle_toolsets` computes — factor the enabled/available computation into a function both call if it is not one already. Models: `resolve_api_key_provider_credentials(provider_id)` → `GET f"{base_url.rstrip('/')}/models"` with `Authorization: Bearer`, using whichever HTTP client the codebase already uses for provider calls (`grep -rn "httpx\|aiohttp.ClientSession" agent/ janus_cli/ | head`); any exception → `(None, f"{type(exc).__name__}: {exc}")`.

Applying: validate the body shape first (empty → 400 `"empty body"`; `raw` with `model`/`agent`/`toolsets` → 400; unknown keys under `model`/`agent` → 400; `api_keys` names against `KEY_NAME_RE` and `_reject_denylisted_env_var` → 400 naming the key). Then, under `_CONFIG_LOCK` if the module exposes it: for `raw`, `yaml.safe_load` → must be a `dict` → `validate_config_structure(mapping)` → errors 400 / warnings kept → `atomic_yaml_write(get_config_path(), mapping, sort_keys=False)`. For the merge: `mapping = yaml.safe_load(text) or {}` of the current file, `_set_nested(mapping, "model.default", ...)` per given key (skip keys whose value is `None`), `toolsets` set whole, validate, write. `api_keys`: `save_env_value(name, value)` or removal for `""`. Return `(applied, warnings)`.

- [ ] **Step 3: The handlers**

```python
    async def _handle_get_config(self, request):
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if api_config.is_managed():
            return web.json_response({"error": "configuration is managed"}, status=409)
        view = await asyncio.to_thread(
            api_config.read_view, version=_janus_version(), restart_pending=self._restart_pending
        )
        return web.json_response(view)

    async def _handle_put_config(self, request):
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if api_config.is_managed():
            return web.json_response({"error": "configuration is managed"}, status=409)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            applied, warnings = await asyncio.to_thread(api_config.apply_change, body)
        except api_config.ConfigChangeError as exc:
            return web.json_response(exc.payload, status=exc.status)
        restarting = False
        if body.get("restart", True):
            if self._restart_handler is None:
                warnings.append("no gateway to restart: the change is on disk and takes effect at the next start")
            else:
                restarting = bool(self._restart_handler())
                self._restart_pending = restarting
        return web.json_response({
            "applied": applied, "warnings": warnings, "restarting": restarting,
            "drain_timeout_seconds": api_config.drain_timeout_seconds(),
        })
```

`_janus_version()` reads the package version the way `/v1/capabilities` or the CLI already does (`grep -rn "__version__\|importlib.metadata" janus_cli/ | head`). `self._restart_pending = False` in `__init__`. `drain_timeout_seconds()` reads `agent.restart_drain_timeout` from `load_config()` through `gateway.restart.parse_restart_drain_timeout`. Register both routes in `connect()` beside `/v1/toolsets`, and add `"config": True` to the capabilities `features` map.

- [ ] **Step 4: Run, lint, commit**

All tests in the new file green; `scripts/run_tests.sh tests/gateway/test_api_server.py` still green; ruff clean. Commit: `feat(api): GET and PUT /v1/config, for a host that configures Janus`.

---

### Task 4: Release 0.17.0

**Files:**
- Modify: `blob-app.json` (`"version": "0.17.0"`), `pyproject.toml` (`version = "0.17.0"`), `README.md` (the Blob section: two sentences — Blob's console can read and change this configuration over `/v1/config`, and sends per-workspace instructions with each run), `CHANGELOG.md` if one exists.

- [ ] **Step 1: Bump and document**, run `.venv/bin/ruff check .` and `scripts/run_tests.sh tests/gateway/test_api_server_config.py tests/gateway/test_api_server.py`, commit: `release: 0.17.0 — a config route for Blob`.
