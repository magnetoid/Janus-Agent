# A config route, per-run instructions, and a restart hook — for Blob

**Status:** design, 2026-09-15. Release **0.17.0**. Decided with Marko the same day, on the
Blob side (`blob/docs/superpowers/specs/2026-09-15-janus-console-design.md`): Blob's
instance console gets a Janus page, and *"Janus gains `PUT /v1/config`"* rather than Blob
writing the file.

## Why

Janus runs inside Blob's Compose stack (`ghcr.io/magnetoid/janus`, profile `janus`), reached
at `http://janus:8642/v1/agui` on an internal network. Its configuration is
`$JANUS_HOME/config.yaml` plus `$JANUS_HOME/.env` for keys — one volume, edited today over
`docker exec`, followed by a restart. The API server exposes `/health`, `/v1/models`,
`/v1/capabilities`, `/v1/skills`, `/v1/toolsets`, `/v1/agui`, chat completions, responses
and runs. Nothing reads or writes configuration, nothing restarts the gateway from
outside, and `/v1/models` answers with the one model Janus is configured as — not what the
provider can serve.

Three additions, all in the API server adapter and the runner, none changing any existing
route:

## 1. `forwardedProps.instructions` on `/v1/agui`

Blob will send a per-workspace instruction text with every run, as
`forwardedProps.instructions` in the AG-UI `RunAgentInput`. `agui_protocol.parse_run_input`
reads it (a string, stripped, at most 4,000 characters; anything else ignored) into
`RunInput.instructions`, and `_handle_agui` folds it into the ephemeral system prompt it
already passes to `_run_agent` — before the context prompt, under a heading that says
where it came from: `Instructions from this workspace's admin:`. A run without it behaves
exactly as today.

## 2. A restart the API server can ask for

`BasePlatformAdapter.set_restart_handler(handler)` beside `set_message_handler`. The runner
extracts the body of its `/restart` command's decision — systemd or a container means
exit 75 through the service manager, otherwise a detached re-exec — into
`GatewayRunner._request_gateway_restart() -> bool`, calls it from `_handle_restart_command`
as before, and sets it as every adapter's restart handler where it sets the message
handler. Only the API server uses it. An adapter with no handler set (the standalone
`janus api` server, not the gateway) reports that it cannot restart rather than failing
the write.

## 3. `GET` and `PUT /v1/config`

Bearer-authenticated with `API_SERVER_KEY` like every other `/v1/*` route. Refused with 409
when `is_managed()` (NixOS / systemd-managed installs), exactly as `janus config set` is.

### `GET /v1/config`

```json
{
  "object": "janus.config",
  "version": "0.17.0",
  "model": {"default": "deepseek-v4-pro", "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1"},
  "agent": {"max_turns": 60, "reasoning_effort": "medium", "gateway_timeout": 1800,
            "personality": "helpful"},
  "personalities": ["helpful", "concise", "technical"],
  "toolsets": {"available": ["janus-cli", "web"], "enabled": ["janus-cli"]},
  "providers": [
    {"id": "deepseek", "name": "DeepSeek", "env": "DEEPSEEK_API_KEY",
     "key": {"set": true, "tail": "a4f2"}},
    {"id": "openai-api", "name": "OpenAI API", "env": "OPENAI_API_KEY",
     "key": {"set": false}}
  ],
  "models": {"provider": "deepseek", "ids": ["deepseek-flash", "deepseek-v4-pro"],
             "reason": null},
  "raw": "model:\n  default: deepseek-v4-pro\n",
  "restart_pending": false
}
```

* `model` and `agent` come from the effective config (`load_config()`), so a value Janus
  is running on shows even when the file does not name it. `agent.personality` is the
  configured default personality name if the config has one, else `null`; `personalities`
  is the keys of `agent.personalities`.
* `toolsets.available` is every resolvable toolset name; `enabled` is what the
  `api_server` platform actually loads (the same computation `/v1/toolsets` does).
* `providers` is every `auth_type == "api_key"` entry of `PROVIDER_REGISTRY`, with the
  first of its `api_key_env_vars` as `env`, and whether a value is present in the process
  environment or `$JANUS_HOME/.env` — `set`, and the last four characters as `tail`. **The
  value itself appears nowhere in any response.**
* `models` is the configured provider's own list: `GET {inference_base_url}/models` with
  the resolved key, ten-second timeout, `data[].id` sorted. Only for API-key providers
  whose base URL speaks the OpenAI shape (DeepSeek and OpenAI do; OpenRouter does);
  otherwise, or on any failure, `ids` is `null` and `reason` says why in one line. Never a
  cache: the point is that a retired name disappears from the list.
* `raw` is `$JANUS_HOME/config.yaml` as text, or `""` when it does not exist.
* `restart_pending` is true after a `PUT` asked for a restart and until the process exits.

### `PUT /v1/config`

```json
{
  "model": {"default": "...", "provider": "...", "base_url": "..."},
  "agent": {"max_turns": 60, "reasoning_effort": "high", "gateway_timeout": 1800,
            "personality": "concise"},
  "toolsets": ["janus-cli", "web"],
  "api_keys": {"DEEPSEEK_API_KEY": "sk-..."},
  "raw": "model:\n  default: ...\n",
  "restart": true
}
```

Every key optional; an empty body is 400. `raw` may not be combined with `model`, `agent`
or `toolsets` (400): a merged edit and a whole-file edit cannot both be the truth. In
order:

1. **`raw`** is `yaml.safe_load`ed; anything but a mapping is 400. It is run through
   `validate_config_structure(mapping)`; any `error`-severity issue is 400 with the issues
   (`{"error": "invalid config", "issues": [{"severity", "message", "hint"}]}`); warnings
   come back in the 200 body. On success the file is replaced through `atomic_yaml_write`
   with `sort_keys=False`.
2. **`model` / `agent` / `toolsets`** are merged into the *raw user file* — read with
   `yaml.safe_load`, not `load_config()`, so no default is ever dumped into the operator's
   file — key by key through `_set_nested` (`model.default`, `model.provider`,
   `model.base_url`, `agent.max_turns`, `agent.reasoning_effort`, `agent.gateway_timeout`,
   `agent.personality` → whatever key the config uses for the default personality,
   `toolsets` as a list). Unknown keys inside `model`/`agent` are 400. The merged mapping
   is validated as in 1 before it is written.
3. **`api_keys`**: each name must match `^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN)$` and pass
   `_reject_denylisted_env_var`; each value goes through `save_env_value`; an empty
   string removes the key from `.env` (add a `remove_env_value` if none exists). Names are
   echoed back under `applied.api_keys`; values never are, and are never logged.
4. **`restart`** (default true) calls the restart handler. Response:
   `{"applied": {"model": {...}, "agent": {...}, "toolsets": [...], "api_keys": ["DEEPSEEK_API_KEY"], "raw": true|false},
   "warnings": [...], "restarting": true, "drain_timeout_seconds": 180}`. With no
   handler, `"restarting": false` and a warning naming why. With `restart: false`,
   `"restarting": false`.

The config cache: `load_config()` is keyed on the file's mtime and size, so a write is
seen by the next read in-process. `restart_pending` is an adapter attribute set when the
handler was called.

## Tests

`tests/gateway/test_api_server_config.py`, on the isolated `JANUS_HOME` the autouse
fixture gives every test, with the adapter built as `test_api_server.py` builds it and the
two routes added to the test app:

* `GET` with no file: `raw == ""`, `model` carries the defaults, every provider `set: false`.
* `GET` after writing a key into `.env`: `set: true`, `tail` is four characters, and the
  value is absent from the serialised body.
* `GET` `models`: the provider's `/models` is fetched through a patched HTTP client and
  its ids returned sorted; a timeout yields `ids: null` with a reason.
* `PUT {"model": {"default": "x"}}` writes that key, leaves an unrelated existing key
  untouched (diff the file), and returns `applied.model`.
* `PUT raw` replaces the file; a scalar is 400; a structure error is 400 with the issue;
  a warning-only file is written and the warning returned.
* `PUT` with `raw` and `model` together is 400; an empty body is 400; an unknown
  `agent` key is 400.
* `PUT api_keys`: `.env` holds the key; the body echoes the name and not the value; a
  name outside the pattern is 400; an empty value removes the key.
* The restart handler is called once and `restarting` is true; with none set,
  `restarting` is false with a warning; with `restart: false`, not called.
* Every route is 401 without the bearer; `is_managed()` patched true gives 409 and an
  untouched file.
* `parse_run_input` reads `forwardedProps.instructions`, ignores a non-string, caps the
  length, and `_handle_agui` passes it inside the ephemeral prompt (assert on the
  `_run_agent` call the way `test_api_server_runs.py` asserts).
* `_request_gateway_restart` chooses `via_service=True` when `/.dockerenv` exists
  (patched) and the detached path otherwise; `_handle_restart_command` still calls it.

## Release

`blob-app.json` and `pyproject.toml` to `0.17.0`. On a green push to main, `image.yml`
publishes `ghcr.io/magnetoid/janus:0.17.0` and `:<sha>`. Blob then pins `0.17.0`.

## Not in this

Anthropic's model list (its `/models` needs its own headers — the text field covers it),
editing skills or SOUL.md, any change to existing routes.
