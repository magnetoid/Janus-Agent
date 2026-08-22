# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical Reference

**`AGENTS.md` (root) is the exhaustive, authoritative development guide — read it.** It covers every subsystem (CLI, TUI, gateway, plugins, skills, toolsets, delegation, curator, cron, kanban, profiles) plus the full catalog of known pitfalls. This file is the fast orientation; AGENTS.md is the depth. `CONTRIBUTING.md` covers cross-platform (Windows) footguns in detail.

This is **Janus Agent** by Cloud Industry — a self-improving AI agent with a learning loop. The directory name (`Janus`) is incidental; the package is `janus-agent`.

## Commands

```bash
# Python env (Python 3.11; requires-python is >=3.11,<3.14)
source .venv/bin/activate            # or: venv/bin/activate
uv pip install -e ".[all,dev]"       # full dev install

# Tests — ALWAYS via the wrapper, never bare pytest (CI-parity matters)
scripts/run_tests.sh                                  # full suite
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh --no-isolate tests/foo/          # faster, for debugging
scripts/run_tests.sh -- -v --tb=long                  # pass-through pytest args

# Lint / typecheck (dev extra)
ruff check .                         # only PLW1514 (unspecified-encoding) is enforced repo-wide
ty check                             # type checker (config in pyproject [tool.ty])

# Run the agent
python run_agent.py --help
janus                               # interactive CLI (entry: janus_cli.main:main)
janus --tui                         # Ink/React TUI (or JANUS_TUI=1)
janus gateway start                 # messaging gateway

# TUI dev (separate JS toolchain)
cd ui-tui && npm run dev | type-check | lint | test    # vitest, tsc, eslint
```

`scripts/run_tests.sh` is mandatory: it unsets credential env vars, pins `TZ=UTC`/`LANG=C.UTF-8`, and runs **each test file in its own spawned subprocess** (no module-level state leakage). Bare `pytest` on a dev box with API keys diverges from CI and has caused repeated "works locally, fails in CI" incidents.

## Architecture (the load-bearing entry points)

The system is a synchronous tool-calling agent loop wrapped in many delivery surfaces. Core dependency chain:

```
tools/registry.py  →  tools/*.py (self-register at import)  →  model_tools.py  →  run_agent.py / cli.py / batch_runner.py
```

- **`run_agent.py`** — `AIAgent` class, the core conversation loop (`run_conversation()`). Synchronous `while` loop with interrupt checks, budget tracking, OpenAI-format messages. ~60-param `__init__`. Agent-level tools (todo, memory) are intercepted here before `handle_function_call()`.
- **`model_tools.py`** — tool orchestration: `discover_builtin_tools()`, `handle_function_call()`, `get_tool_definitions()`. Importing this triggers tool *and* plugin discovery as a side effect.
- **`toolsets.py`** — `TOOLSETS` dict + `_JANUS_CORE_TOOLS`. A registered tool is only *exposed to an agent* if its name appears in a toolset (registration ≠ exposure).
- **`cli.py`** — `JanusCLI`, interactive CLI orchestrator (Rich + prompt_toolkit). `process_command()` dispatches via the central command registry.
- **`janus_state.py`** — `SessionDB`, SQLite session store with FTS5 search.
- **`janus_constants.py`** — `get_janus_home()` / `display_janus_home()`, profile-aware paths.

Delivery surfaces fan out from the same agent core:
- **`gateway/`** — single-process messaging gateway; `platforms/` has one adapter per messaging platform (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, email, …).
- **`ui-tui/`** (Ink/React) + **`tui_gateway/`** (Python JSON-RPC backend) — the TUI. TS owns the screen; Python owns sessions/tools/models. The `janus dashboard` web chat and `apps/desktop/` Electron app are *separate* surfaces — see AGENTS.md before touching either.
- **`acp_adapter/`** — ACP server for VS Code / Zed / JetBrains.
- **`cron/`**, plus `tools/delegate_tool.py` (subagents), the curator (skill lifecycle), and kanban (multi-agent work queue).

### Extension surfaces — prefer these over editing core

- **New tools:** for custom/local tools use a plugin (`~/.janus/plugins/<name>/`), not core. A *core* tool needs 2 edits: create `tools/your_tool.py` with a top-level `registry.register(...)` (auto-discovered), then add its name to a toolset in `toolsets.py`.
- **Plugins** (`plugins/`, `janus_cli/plugins.py`): three discovery systems — general (`PluginManager`), memory-providers (`plugins/memory/`, lazy), model-providers (`plugins/model-providers/`, lazy, last-writer-wins). **Plugins MUST NOT modify core files** — extend the generic hook/ctx surface instead. No new in-tree memory or model-provider directories (closed sets; publish as standalone plugin repos).
- **Slash commands:** add a `CommandDef` to `COMMAND_REGISTRY` in `janus_cli/commands.py` (everything downstream — CLI/gateway/Telegram/Slack/autocomplete — derives from it), then handlers in `cli.py` and `gateway/runner.py` (`gateway/run.py` is a 35-line launcher shim — not where handlers live).
- **Skins (CLI look-and-feel):** pure data, no code — `janus_cli/skin_engine.py` holds `SkinConfig` + built-ins; user skins are drop-in YAML under `<janus_home>/skins/`. Missing values inherit from `default`. The default is the minimal refined-gold skin (old look: `/skin classic`); don't hardcode colors/emojis in `banner.py`/`display.py`/`cli.py` — add a skin key. See AGENTS.md → "Skin/Theme System".
- **Config:** non-secret settings go in `config.yaml` via `DEFAULT_CONFIG` (`janus_cli/config.py`); secrets only go in `.env` via `OPTIONAL_ENV_VARS`. Three config loaders exist (`load_cli_config`, `load_config`, raw gateway YAML) — adding a key to the wrong place makes CLI and gateway disagree.

## Non-obvious invariants (do not violate)

- **Prompt caching must not break.** Never alter past context, change toolsets, or rebuild system prompts mid-conversation. The only sanctioned context mutation is compression. Slash commands that mutate system-prompt state must be cache-aware: default to deferred invalidation, opt-in `--now` for immediate. This is a core cost/correctness constraint.
- **Never hardcode `~/.janus` / `Path.home() / ".janus"`.** Use `get_janus_home()` for code paths and `display_janus_home()` for user-facing strings — hardcoding breaks profiles (isolated multi-instance support). Tests must not write to real `~/.janus` (the `_isolate_janus_home` autouse fixture redirects it).
- **Dependency pinning is exact (`==X.Y.Z`) for core deps**, with upper bounds everywhere, established after real supply-chain attacks (litellm, Mini Shai-Hulud). Provider-specific deps live in extras and lazy-install via `tools/lazy_deps.py` — only universally-used packages belong in core `dependencies`. Regenerate `uv.lock` with `uv lock` after any change.
- **Don't write change-detector tests** (snapshots of model catalogs, config version literals, enumeration counts). Assert *relationships/invariants* instead — reviewers reject snapshot tests.
- Additional pitfalls (gateway double message-guard, `simple_term_menu` ban, `\033[K` spinner bug, cross-tool schema references, stale-branch squash reverts) are documented in AGENTS.md → "Known Pitfalls."
