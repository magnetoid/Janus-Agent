# CI Test Baseline — Pre-existing failures to drive to zero

*Established 2026-07-10 when CI was first added (Track 1 Phase 0.1 of
[`self-improvement-evaluation-2026.md`](self-improvement-evaluation-2026.md), gap G2).*

## Why this file exists

Janus had **no CI** before [`.github/workflows/tests.yml`](../.github/workflows/tests.yml).
The full suite is run only on maintainer dev boxes, which carry environment
state (config, credentials, installed extras, a populated `~/.janus`) that a
clean CI runner does not. The first CI run therefore surfaced a large batch of
failures that are **pre-existing** — every one reproduces on `main` at the
commit CI was added, none was introduced by the Phase 0 work — of the exact
"works locally, fails in CI" class `CLAUDE.md` warns about.

The `tests-full` job runs **informationally** (`continue-on-error: true`) so
this baseline stays visible and shrinkable without blocking merges. The gate
becomes hard once the count reaches zero. `lint` and `tests-changed` are hard
gates today.

## Failure categories (from the clean `.[all,dev]` run)

1. **Change-detector tests that broke on legitimate refactors** — e.g.
   `test_windows_native_support.py::test_readme_mentions_powershell_installer`
   asserts `install.ps1` appears in `README.md` (the README was rewritten in
   `8857e06` and dropped it); `test_terminal_config_env_sync.py` asserts a
   `_terminal_env_map = {…}` literal exists in source (refactored away). These
   violate the repo's own "no change-detector tests" policy (`CLAUDE.md`) and
   are the cheapest wins — fix by asserting the invariant, not the text.
2. **Headless-approval env gap** — `test_code_execution.py` etc. expect
   `execute_code` to run, but the approval guard blocks it in a non-interactive
   context. Needs the test to set `approvals.headless_mode: approve` (or mock
   the guard).
3. **Missing test config / state divergence** — dashboard, gateway/service,
   model-picker, LSP, and honcho suites that depend on env/config a dev box
   has and a clean runner does not.

## The baseline list

<!-- BASELINE_LIST_START -->
39 files, from the first informational `tests-full` run (clean Ubuntu runner,
`.[all,dev]`, CI run 29099061312). Check a box when the file passes clean.

- [x] `tests/agent/test_embeddings.py` *(needs `numpy` — the `voice`/embeddings optional dep)*
- [x] `tests/cli/test_personality_none.py`
- [x] `tests/cron/test_codex_execution_paths.py`
- [x] `tests/janus_cli/test_aux_config.py`
- [x] `tests/janus_cli/test_gateway_restart_loop.py`
- [x] `tests/janus_cli/test_kanban_core_functionality.py`
- [x] `tests/janus_cli/test_prompt_size.py`
- [x] `tests/janus_cli/test_setup_reconfigure.py`
- [x] `tests/janus_cli/test_startup_plugin_gating.py`
- [x] `tests/janus_cli/test_status.py`
- [x] `tests/janus_cli/test_terminal_menu_fallbacks.py`
- [x] `tests/janus_cli/test_tool_token_estimation.py`
- [x] `tests/janus_cli/test_tools_config.py`
- [x] `tests/janus_cli/test_update_autostash.py`
- [x] `tests/janus_cli/test_web_server.py`
- [x] `tests/janus_cli/test_web_server_cron_profiles.py`
- [x] `tests/janus_cli/test_web_server_session_search.py`
- [x] `tests/janus_cli/test_web_ui_build.py`
- [x] `tests/plugins/test_kanban_dashboard_plugin.py`
- [x] `tests/run_agent/test_anthropic_prompt_cache_policy.py`
- [x] `tests/run_agent/test_background_review_toolset_restriction.py`
- [x] `tests/run_agent/test_percentage_clamp.py`
- [x] `tests/run_agent/test_tool_arg_coercion.py`
- [x] `tests/test_janus_bootstrap.py`
- [x] `tests/test_lint_config.py`
- [x] `tests/test_model_tools.py`
- [x] `tests/test_sanitize_tool_error.py`
- [x] `tests/test_timezone.py`
- [x] `tests/test_tui_gateway_server.py`
- [x] `tests/tools/test_approval.py`
- [x] `tests/tools/test_code_execution.py` *(headless-approval env gap)*
- [x] `tests/tools/test_code_execution_modes.py`
- [x] `tests/tools/test_cron_approval_mode.py`
- [x] `tests/tools/test_delegate_composite_toolsets.py`
- [x] `tests/tools/test_execute_code_approval_cluster.py`
- [x] `tests/tools/test_hardline_blocklist.py`
- [x] `tests/tools/test_modal_sandbox_fixes.py`
- [x] `tests/tools/test_terminal_config_env_sync.py` *(change-detector: source-literal introspection)*
- [x] `tests/tools/test_terminal_tool_requirements.py`
- [x] `tests/tools/test_windows_native_support.py` *(change-detector: README text pin)*
<!-- BASELINE_LIST_END -->

## Status: all 40 cleared (2026-07-30)

Every file above now passes under `scripts/run_tests.sh`. What the sweep found,
by category:

- **Change-detector tests** (the largest group, exactly as predicted): asserting a
  source literal, a README phrase, a toolset enumeration, or a config-shape rule
  that a legitimate refactor moved. Rewritten to assert the invariant — e.g.
  `test_terminal_config_env_sync.py` stopped `ast.parse`-ing production source for a
  `_terminal_env_map = {…}` literal and now drives the real config→env bridges,
  going from 9 tests to 41 in the process.
- **Headless-approval env gap**: `execute_code` is blocked by
  `check_execute_code_guard()` because `approvals.headless_mode` defaults to `deny`
  and a pytest process is headless. Fixed by writing that key into the isolated
  `JANUS_HOME` config, so the guard still runs and is still exercised.
- **Stale assumption — tool discovery**: several suites assumed `import model_tools`
  self-registers every tool. It has not since `70dbbfa` moved
  `discover_builtin_tools()` out to each entry point. They now call it the way the
  entry points do.
- **Stale assumption — the `gateway.run` shim**: tests patched names on
  `gateway/run.py`, which only *copies* them out of `gateway.core`/`gateway.runner`
  at import. Patching the copy is a no-op, so several tests were passing or failing
  for reasons unrelated to what they claimed to test.

**Four were real production bugs, not test defects** (fixed in the same commit):

| Bug | Impact |
|---|---|
| `agent_runtime_helpers.py` still matched the pre-rebrand `imbalabs` domain | Portal traffic got 0% prompt-cache hits and re-billed the full prompt every turn |
| `.janus-bootstrap-complete` was not in `.gitignore` | `janus update`'s `git stash -u` swept it, re-triggering bootstrap on every update (#38529) |
| `auxiliary.dialectic_{advocate,skeptic,arbiter}` missing from `DEFAULT_CONFIG` | The three dialectic stances could not be configured or model-pinned |
| `autonomy` / `self-improve` missing from `_BUILTIN_SUBCOMMANDS` | Plugin discovery ran eagerly on two built-in commands that should skip it |

## The gate is now hard (2026-07-30)

Clearing this list was necessary but not sufficient — the *full* suite had 63 more
failures across 18 files outside it. Those were fixed too (`3984a77`, `ae191cc`), and
`tests-full` then reported **1455 files / 30,377 tests / 0 failed** on the Ubuntu
runner, so `continue-on-error` came off. `tests-full` blocks merges now.

Nearly all of that second wave was the *inverse* of this baseline: tests that passed
on the Linux runner and failed on a macOS dev box, so nobody saw them. The recurring
causes are worth knowing, because they are easy to reintroduce:

- **Host-shaped paths.** `/tmp` → `/private/tmp` and `/var` → `/private/var` on macOS.
  One test's fake config landed under `/private/var/`, which is legitimately in
  `_SENSITIVE_PATH_PREFIXES`, so the generic rule fired before the one under test.
- **Credential sources that dodge patching.** `read_claude_code_credentials()` checks
  the macOS Keychain *before* the JSON file, via `security`, ignoring `Path.home()`
  patching — the developer's real OAuth token flowed into assertions.
- **Platform probes left unmocked.** `shutil.which("systemctl")`, the user D-Bus
  preflight, `/proc` zombie checks. Mock the probe, not just the branch above it.
- **Timestamp granularity.** `_web_ui_build_needed` compares with `>=`; two files
  created microseconds apart share an mtime on a coarser filesystem. Backdate
  deliberately instead of relying on call ordering.
- **Tests that install things.** One file ran a real `pip install modal` mid-suite.
  No test may reach the network.

Also worth revisiting: a few subprocess-heavy tests (`test_code_execution.py`,
`test_kanban_core_functionality.py`) sit near the 30s per-test cap and time out when
the machine is starved. They pass comfortably otherwise, but now that this gate
blocks, that headroom matters — use `scripts/run_tests.sh -j 8` on a busy box.

## If tests-full goes red

- Reproduce the file alone first: `scripts/run_tests.sh <path>` (the wrapper's
  hermetic env reproduces CI locally). Many one-off failures are load-induced
  timeouts, not real.
- If it persists, `git stash -u`, re-run, `git stash pop` to confirm it is yours.
- Fix the test or the code. Do **not** re-add `continue-on-error` — that silently
  recreates the baseline this file documents removing.
