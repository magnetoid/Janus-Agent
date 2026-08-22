# Janus Recursive Self-Improvement — Post-Implementation Audit & Two-Track Feasibility Evaluation

*Produced 2026-07-10. Successor to [`docs/agi-roadmap-2026.md`](agi-roadmap-2026.md), written after all 10 of that roadmap's moves shipped. Every load-bearing claim below was re-verified against the code at `main` (`8857e06`) — citations are `file:line` from this verification pass, not carried over from the older document. Verification log in Appendix A.*

---

> ## ⚠️ Status update — re-verified 2026-07-31 (read this before acting on anything below)
>
> **Seven of the twelve gaps in §2.5 have been closed since this audit was written.** The
> body text below is preserved as the 2026-07-10 snapshot; the **Status column in §2.5 is
> authoritative**, and §5's phase tables are annotated. What changed:
>
> - **G1 (no proposer) — CLOSED.** [`agent/proposer.py`](../agent/proposer.py) exists and is
>   wired into the sleep cycle (`agent/sleep.py:381`), with
>   [`agent/eval_orchestrator.py`](../agent/eval_orchestrator.py) evaluating what it records
>   and [`agent/twin_review.py`](../agent/twin_review.py) red-teaming it (`sleep.py:405`).
>   The headline finding of this audit — "`propose()` has zero autonomous production
>   callers" — **is no longer true.** Phase 2.1 *and* 2.2 shipped.
> - **G2 (no CI) — CLOSED.** [`.github/workflows/tests.yml`](../.github/workflows/tests.yml)
>   runs three hard gates: `lint` (ruff), `tests-changed`, `tests-full` (~30.4k tests, full
>   suite, no `continue-on-error`). "The most load-bearing absence in the repo" is filled.
> - **G3 (red-team bypass) — CLOSED.** `lessons.screen_lesson` (`agent/lessons.py:150-183`)
>   routes reflexion and compression-sink lessons through `red_team_claims` before they
>   persist; it fails open on infra error but marks the record unvetted.
> - **G4 (store races) — CLOSED.** [`agent/store_lock.py`](../agent/store_lock.py) gives all
>   learning stores a real cross-process `flock` plus `os.replace` atomic write.
> - **G8 (kanban injection) — CLOSED.** `_scan_kanban_task_for_injection`
>   (`janus_cli/kanban_db.py:6035`) blocks and audits at claim time (`:6343`).
> - **G11 (default-off adoption) — CLOSED.** [`janus_cli/learning_onboarding.py`](../janus_cli/learning_onboarding.py)
>   offers the read-only bundle once on first run (`janus_cli/main.py:2166`); write-side
>   flags stay individually opt-in, exactly as Phase 2.4 specified.
> - **G12 (no audit stream) — CLOSED.** [`agent/audit_log.py`](../agent/audit_log.py) is a
>   hash-chained append-only JSONL stream with `verify()`, consumed by `self_improve`,
>   `twin_review`, `autonomy_guard`, `proposer`, and `kanban_db`.
>
> **G7 is partially closed:** governor FROZEN now pauses mining/lesson writes
> (`agent/auto_mine.py:62`) and the promotion path calls `regression_gate(fail_closed=True)`
> (`agent/self_improve.py:363`), so the fail-open *inversion* is fixed. What remains is that
> the gate's own default is still fail-open and CI does not yet run `janus evals gate`.
>
> **G5 — CLOSED, but it was worse than recorded before it was fixed.** The context-sensitive
> guard had shipped as *dead code*: `DEFAULT_CONFIG` carried a literal `False`, and because
> `load_config` deep-merges the defaults, that literal was indistinguishable from a user
> setting and shadowed the headless default on every install. Every test stubbed
> `load_config` with a dict that omitted the key, so nothing caught it. The default is now
> the `"auto"` sentinel.
>
> **G10 — CLOSED 2026-08-01.** The shaped reward now gates promotion alongside the
> boolean, and the governor tightens both bars under CAUTION. **G6 and G9 remain open.**
>
> Running tally, counted off the table above: **8 of 12 closed** (G1–G5, G8, G10, G11),
> **2 partial** (G7 gate default + missing CI eval step; G12 vector tier + trajectory
> consumer), **2 open** (G6 no sandbox by default, G9 spend caps blind to unpriced models).
>
> Consequently the §1 scorecard rows for *CI / change management* (1/5) and
> *Self-modification* (2/5), the §2.3 "MISSING proposer" diagram, and the §3.2 prerequisite-1
> blocker are all **superseded**. Track 2's go/no-go is no longer "all seven ✗".

---

## 1. Executive summary

> **⚠️ Superseded 2026-07-31 — the generator is now wired.** The paragraph below was the
> headline finding on 2026-07-10 and is kept for the record, but `propose()` today has
> autonomous callers: `agent/proposer.py` runs as a sleep-cycle step (`sleep.py:381`),
> `agent/eval_orchestrator.py` evaluates the variants, and `agent/twin_review.py` supplies
> proposer/approver separation (`sleep.py:405`). Read "the single most consequential finding"
> as *historical*.

**Track 1 — the artifact-level self-improvement loop (skills / prompts / policies): wire and harden. ~90% built; the generator is unwired.** Janus ships a complete experience→lesson→recall→outcome→consolidation pipeline, a statistical eval spine, a health governor, a spend-cap + kill-switch safety floor, and a DGM-lite self-modification engine with lineage and rollback. The single most consequential finding of this audit: **`agent/self_improve.py::propose()` has zero autonomous production callers.** The gate, archive, and rollback half of "the agent rewrites itself" is finished and well designed; the half that *generates* proposals — an orchestrator, a sleep-cycle step, a cron job, anything — does not exist. The module's own docstring acknowledges it ("the two seams an orchestrator (or a test) drives", `agent/self_improve.py:23-26`). Until that edge is wired, "recursive" self-improvement is a CLI-reviewable data store with nothing flowing into it.

**Track 2 — core-Python-code self-modification: conditional go.** The prohibition on the agent touching core code (`agent/self_improve.py:10-13`) is correct *today* because every prerequisite for doing it safely is currently unmet — most notably **there is no CI at all** (`.github/` does not exist; zero tracked workflow files) even though scripts and docs reference workflows by path (`scripts/run_tests_parallel.py:59-61`). The go/no-go checklist in §6 has seven prerequisites; all seven are currently ✗. With them met, a phased contributor→gatekeeper path (SICA / Live-SWE-agent pattern) is feasible on this codebase, which has unusually strong raw material: ~1,459 test files with hermetic per-file subprocess isolation, a shadow-git checkpoint/rollback system, cheap fully-isolated profiles, and containerized execution backends.

**The twin-core ("two-headed Janus") architecture is adopted as the recommended shape for both tracks** — two isolated cores (profiles) that propose, critique, and red-team improvements *for each other*, while promotion always passes the existing deterministic gate stack plus human approval. Asymmetric roles only: mutual proposal is the feature; mutual *approval* is banned (§4.4).

### Scorecard delta vs. `agi-roadmap-2026.md`

| Subsystem | Then | Now | What changed |
|---|---|---|---|
| Learning stack (lessons/outcomes/sleep/governor) | 3/5 | **4/5** | Loop closed: push recall per turn, efficacy credit/debit, unattended sleep graduation. Docked: red-team gate bypassed by two lesson sources; stores race. |
| Measurement (evals/trend) | 3/5 | **4/5** | pass^k, capability/regression kinds, noise floor, time-horizon KPI. Docked: gate is fail-open by default and wired to no CI. |
| Autonomy safety | 3/5 | **4/5** | `AUTONOMY_FROZEN` kill switch + rolling USD caps enforced at cron/kanban/delegate/self-improve. Docked: gates new work only; unpriced models accrue $0. |
| Self-modification (DGM-lite) | 0/5 | **2/5** → **4/5** *(2026-07-31)* | Gate/archive/rollback complete and verified. ~~Generator absent; hence 2, not 4.~~ **Generator shipped** (`agent/proposer.py` + `eval_orchestrator.py` + `twin_review.py`, sleep-wired). Docked to 4: core code still out of scope and human approval still required. |
| Model routing | 2/5 | **3/5** | Canonical taxonomy, EWMA, cost-joined ranking, wired into delegation with learned-distrust gate. |
| Security posture for autonomy | 3/5 | **3/5** | Unchanged: no default sandbox; agent-created skills unscanned by default; kanban bodies unscanned. |
| CI / change management | 1/5 | **1/5** → **4/5** *(2026-07-31)* | ~~Unchanged: no CI exists. The most load-bearing absence in the repo.~~ **CI shipped and hard-gating** (`lint` + `tests-changed` + `tests-full`, ~30.4k tests). Docked to 4: `janus evals gate --fail-closed` and `ty check` are still deliberately outside the gate. |

---

## 2. Codebase audit *(component 1)*

### 2.1 Architecture recap

Full subsystem detail lives in `AGENTS.md` and `docs/agi-roadmap-2026.md` Appendix A; this section records only the load-bearing shape, re-confirmed this pass.

- **Core loop.** `run_agent.py::AIAgent` (235 KB) runs a synchronous while-loop over OpenAI-format messages: transport call → tool dispatch → repeat until no tool calls / iteration budget (90 parent, 50 subagent) / interrupt. Tool chain: `tools/registry.py` (self-registration at import) → `model_tools.py` (schema assembly + `handle_function_call`) → `toolsets.py` (registration ≠ exposure; a tool must appear in a toolset). Providers: 4 transports (`agent/transports/`: chat_completions, anthropic_messages, codex_responses, bedrock_converse) + ~29 model-provider plugins. No litellm — single pinned `openai==2.24.0` SDK.
- **Storage.** Sessions: `janus_state.py::SessionDB` (SQLite + FTS5, silent degradation without FTS5). Learning stores: flat JSON under `$JANUS_HOME/learning/` — `lessons.json`, `outcomes.json`, `model_strengths.json`, `feedback_signals.json`, `self_improve.json`, plus `cost_ledger.jsonl`. Memory: char-capped `MEMORY.md`/`USER.md` snapshot + daily journal + 8 optional provider plugins. **No persistent vector index** — optional fastembed re-rank + RRF fusion (`agent/embeddings.py:97-133`) over a lexical top-N pool only.
- **Delivery surfaces** all funnel into the same `AIAgent`: CLI (`cli.py`, 741 KB), Ink TUI + `tui_gateway/` JSON-RPC (80+ methods), messaging gateway (~24 platform adapters; `gateway/runner.py` is 880 KB), ACP adapter, OpenAI-compatible HTTP API, MCP server.
- **Local-first.** Everything under `$JANUS_HOME` (`janus_constants.get_janus_home()`), profiles give full instance isolation cheaply. No mandatory cloud service; Docker/SSH/Modal/Daytona/Singularity execution backends are opt-in (`tools/environments/`).

### 2.2 The nine self-improvement subsystems — verified inventory

| # | Subsystem | Module | Verified mechanics | Grade |
|---|---|---|---|---|
| 1 | **Lessons** | `agent/lessons.py` | Reflexion failure→lesson (via `agent/auto_mine.py`), 300-cap store, stable IDs + `helpful`/`harmful` efficacy counters credited/debited per session outcome. **Push recall**: `recall_context_for_turn` (`lessons.py:426-434`) computed once per turn and injected into the current user message at API-call time — never persisted, never the system prompt (`agent/conversation_loop.py:828-840`, assembly `:1030-1045`). | 4/5 |
| 2 | **Outcome tracking** | `agent/outcome_tracker.py` | LLM-judged SUCCESS/FAILURE per session; boolean `skill_success_trajectory` (`:124`) plus continuous tool-failure-penalized `skill_reward_trajectory` (`:133-136`). Continual-learning metrics (forgetting, diversity trend, forward transfer). | 4/5 |
| 3 | **Sleep consolidation** | `agent/sleep.py` | `run_sleep_cycle` (`:253`): GRADUATE requires injected sessions (`:274-288`) — now fed unattended; PROMOTE opt-in + governor-gated (`:290-299`); SYNTHESIZE red-team-gated (`:186-237`). Fires at CLI exit + gateway idle. | 4/5 |
| 4 | **Governor** | `agent/self_improvement_governor.py` | OK/CAUTION/FROZEN from learning metrics + eval trend. Freezes on eval-gate failure (`:134-136`) and forgetting/diversity collapse (`:158-165`). **Deliberately fails OPEN on internal error** (`:15-20`, `:180-182`). Consumers: `agent/skill_graph.py:324`, `agent/sleep.py:294`, `self_improve.can_promote`. | 3/5 |
| 5 | **Autonomy safety floor** | `agent/autonomy_guard.py` | `blocked_reason()` = sentinel-file/config freeze + rolling USD caps. Enforced at `tools/delegate_tool.py:2016`, `cron/scheduler.py:2042`, `janus_cli/kanban_db.py:6106`, `agent/self_improve.py:334`. Honest-scope docstring (`:17-28`): gates **new** work only; **unpriced models accrue $0**; `blocked_reason` fails open — the sentinel file is the reliable layer. | 4/5 |
| 6 | **Eval spine** | `agent/evals.py`, `agent/eval_trend.py` | Specs are YAML in `$JANUS_HOME/evals/` (`evals.py:84-85`); pass^k, capability/regression kinds, noise floor, time-horizon KPI. `regression_gate(fail_closed=False)` **defaults fail-open** ("the default stays fail-open for cron", `eval_trend.py:347-359`); `fail_closed=True` is reserved for CI — which does not exist. | 4/5 |
| 7 | **DGM-lite self-modification** | `agent/self_improve.py` | See §2.3. Gate complete; generator absent. | 2/5 |
| 8 | **Learned routing** | `agent/model_routing.py`, `agent/model_strengths.py` | Canonical task taxonomy, EWMA + sample counts + ledger-joined mean cost; wired into `delegate_tool` with a learned-distrust gate; decisions only at task-entry boundaries (cache-safe). | 3/5 |
| 9 | **Implicit-feedback sensors** | `agent/feedback_signals.py` | Interrupt/denial/edit/correction/steer per session → friction score. **Observability-only by explicit design** (not reward — sign unvalidated, attribution confounded). Only learning store using atomic writes (`:93-104`). | 4/5 |

Supporting cast: curator (agent-created-skills-only lifecycle, archive-never-delete, snapshot-before-run), skill graph (triple-gated draft promotion), dialectic red-team (`agent/deliberation.py::red_team_claims`), self-challenge (deterministic-check self-play), cost ledger, checkpoint manager (shadow-git rollback, invisible to the LLM — `tools/checkpoint_manager.py:1-12`).

### 2.3 The unwired recursive half *(closed 2026-07-31 — see banner)*

> The diagram and grep result below describe the 2026-07-10 state. The MISSING box is now
> filled by `agent/proposer.py` → `agent/eval_orchestrator.py` → `agent/twin_review.py`,
> all driven from `run_sleep_cycle` and triple-gated (`self_improve.enabled`, governor not
> FROZEN, autonomy floor clear) so a default install still proposes nothing.

The intended loop:

```
        ┌──────── WAS MISSING (shipped) ───────┐
        │ agent/proposer.py → eval_orchestrator │
        │      → twin_review  (sleep-wired)     │
        └────────────────┬─────────────────────┘
                         ▼
 propose() ──▶ record_evaluation() ──▶ approve() ──▶ promote() ──▶ rollback()
 (0 callers)   (strict improvement     (human)       (6-condition   (backup-
                required, refuses                     gate, §2.4)    restore)
                re-eval of promoted)
```

Verified: repo-wide grep finds `agent/self_improve.py` referenced in production only by the CLI review commands (`janus self-improve list/show/approve/promote/rollback`, `janus_cli/main.py:15714-15792`) and config defaults (`janus_cli/config.py:1839-1850`). `agent/sleep.py:294` and `agent/skill_graph.py:324` import the *governor*, not the proposal engine. `run_sleep_cycle` never calls `self_improve`. No cron job, no tool, no GEPA-style evolution loop generates proposals.

This is the difference between the README's claim ("it can propose careful improvements to its own skills and prompts") and reality: the *capability to accept and gate* such proposals exists; nothing proposes.

### 2.4 The gate stack (what promotion actually requires)

`can_promote` (`agent/self_improve.py:300-345`) — six conditions, each refusal-by-default:

1. `learning.self_improve.enabled` on (`:303`; default off, `janus_cli/config.py:1846`).
2. Proposal status `evaluated`/`approved` with `gate_ok` — a **strict** measured improvement, `score_after > score_before`, so a fabricated 0→0 pair fails (`:276`, `:310`).
3. Target re-validation against the allowlist — kinds `(skill, prompt_fragment, lesson_policy)` (`:38`), roots `skills/.drafts | prompts | policies` under `$JANUS_HOME` only (`:42-46`), `..`/absolute/symlink escapes refused with `O_NOFOLLOW` writes (`resolve_target` `:87-108`).
4. Live eval-suite regression cross-check — **fails closed** (`:318-324`), explicitly to defeat the DGM fabricated-evidence failure mode.
5. Governor not FROZEN — **fails closed** at this call site (`:326-331`) even though the governor itself fails open internally.
6. Autonomy floor clear — **fails closed** (`:333-339`) — plus human approval unless deliberately graduated (`require_human_approval` defaults True even on garbled config, `:80-82`).

Promotion backs up before writing (never overwrites an existing backup, `:362-371`); `rollback` restores or removes (`:379-400`). This layering — advisory components fail open, the promotion gate fails closed — is the right asymmetry and should be named as the design rule elsewhere (it is currently violated by the eval gate default and the governor's own scope; see gaps G2, G7).

### 2.5 Verified gap register

*Severity and Evidence are as of 2026-07-10. **Status is as of 2026-07-31** and is the
authoritative column — where the two disagree, Status wins.*

| ID | Gap | Evidence (2026-07-10) | Severity | Status (2026-07-31) |
|---|---|---|---|---|
| **G1** | No autonomous proposer feeding `self_improve.propose()` | grep: zero non-CLI callers; docstring `self_improve.py:23-26` | **Critical** (capability) | ✅ **CLOSED** — `agent/proposer.py` wired at `sleep.py:381`; `agent/eval_orchestrator.py` scores variants; `agent/twin_review.py` red-teams at `sleep.py:405` |
| **G2** | No CI exists — no `.github/` on disk or in git — while scripts/docs cite workflows by path | `git ls-files` → 0; `scripts/run_tests_parallel.py:59-61` | **Critical** (integrity) | ✅ **CLOSED** — `.github/workflows/tests.yml`: `lint` + `tests-changed` + `tests-full`, all hard gates |
| **G3** | Reflexion lessons and the compression sink bypass the dialectic red-team gate; both feed per-turn recall | red_team only in `playbook.py:229-236`, `sleep.py:186-237`; lesson writers `auto_mine.py:104-110`, `conversation_compression.py:271`; injection surface `conversation_loop.py:828-840` | **High** (security) | ✅ **CLOSED** — `lessons.screen_lesson` (`lessons.py:150-183`) gates both sinks through `red_team_claims`; infra error → fail open + record marked unvetted |
| **G4** | Learning stores race: plain `write_text` read-modify-write, no locking, across automine threads / sleep / gateway | `lessons.py:83-86,144-151`, `outcome_tracker.py:50-53`, `model_strengths.py:89`; only `feedback_signals.py:93-104` is atomic | High | ✅ **CLOSED** — `agent/store_lock.py`: `flock`/`msvcrt` exclusive lock + `os.replace` atomic write, used by all three stores |
| **G5** | Agent-created skills load unscanned by default | `skill_manager_tool.py:59-60` (`guard_agent_created` default False), `skills_guard.py:58-59` | High | ✅ **CLOSED 2026-07-31.** Found dead on re-verification: the context-sensitive resolver existed but `DEFAULT_CONFIG` hardcoded `guard_agent_created: False`, and since `load_config` deep-merges the defaults, `cfg_get(..., default=None)` read that literal as an explicit user choice and `_no_human_present()` was never consulted. Every test stubbed `load_config` with a dict that *omitted* the key — the one shape production never produces. Fixed by making the default the sentinel `"auto"`, plus a `migrate_config` step (v29) that reclaims the `false` already materialized in existing users' `config.yaml` — without it the fix would have reached fresh installs only, since `save_config` writes the full merged config to disk. Pinned by `tests/tools/test_skill_guard_headless.py`, which exercises the real `load_config`. Generalized as a pitfall in `AGENTS.md` |
| **G6** | No sandbox by default for autonomous execution; SECURITY.md itself says only the OS is a boundary | SECURITY.md §2.2; `tools/environments/local.py` default | High | ⬜ **OPEN** — unchanged |
| **G7** | Eval regression gate defaults fail-open; fail-closed mode exists but is wired to nonexistent CI; governor fails open and its FROZEN state only blocks promotion (lesson writes, mining, model_strengths continue) | `eval_trend.py:347-359,407-408`; `self_improvement_governor.py:15-20,180-182` | High | 🟡 **PARTIAL** — the inversion is fixed: `learning_frozen()` now pauses mining/lesson writes (`auto_mine.py:62`), and promotion calls `regression_gate(fail_closed=True)` (`self_improve.py:363`). Still open: gate default remains fail-open (`eval_trend.py:347`) and CI does not run `janus evals gate` |
| **G8** | Kanban task bodies/comments are not injection-scanned (cron prompts are) | scanning only in `cron/scheduler.py:49-57,1220,1294-1297`; none in `kanban_db.py`/`kanban_tools.py` | Medium-High | ✅ **CLOSED** — `_scan_kanban_task_for_injection` (`kanban_db.py:6035`) blocks + audits at claim time (`:6343-6353`) |
| **G9** | Spend caps blind to unpriced models ($0) and to in-flight work | `autonomy_guard.py:17-28` (documented honestly) | Medium | ⬜ **OPEN** — unchanged; still documented honestly in-module |
| **G10** | Continuous shaped reward has no consumer — promotion still uses the boolean trajectory | `skill_reward_trajectory` defined `outcome_tracker.py:133`; `skill_graph.py:252,268` uses `skill_success_trajectory` | Medium | ✅ **CLOSED 2026-08-01.** `assess_promotability` now gates on BOTH signals: the boolean success rate and a shaped `mean_reward` floor (`graph.promotion_reward_threshold`, default 0.6). The boolean is blind by construction to a skill whose every session succeeded only after most of its tool calls failed — that reads as 100%, while `reward = success − 0.5×tool_failure_rate` reads 0.5. The governor tightens the new bar under CAUTION too (`caution_reward_floor`, 0.75), so no promotion bar sits at its relaxed default when the loop looks shaky. `mean_reward` is returned in the assessment for observability |
| **G11** | Everything default-off; a stock install accumulates nothing until `janus learning enable` (which correctly flips only the read-only bundle, `janus_cli/main.py:15500-15540`) | by design, but adoption-limiting | Medium | ✅ **CLOSED** — `janus_cli/learning_onboarding.py` offers the read-only bundle once at first run (`main.py:2166`); write-side flags stay opt-in |
| **G12** | No unified tamper-evident audit stream; no persistent vector/archival memory tier; trajectory export has no consumer; `plans/self-improvement-roadmap.md` status header is stale | various (see §5 Phase 0/3) | Low-Medium | 🟡 **MOSTLY CLOSED** — `agent/audit_log.py` is hash-chained + append-only with `verify()`; roadmap header corrected. Still open: no persistent vector tier, trajectory export still unconsumed (Phase 3) |

Cache-invariant note: partial regression coverage now exists (`tests/run_agent/test_background_review_cache_parity.py`, `tests/agent/test_system_prompt_restore.py`), but the end-to-end cache-prefix **byte-stability** test recommended by the Track D guidance is still absent.

---

## 3. Technical prerequisites *(component 2)*

### 3.1 Track 1 (artifact loop) — all incremental, no architectural change needed

- **A proposer** (G1). Cheapest first lever: a sleep-cycle step that turns eval-trend regressions and synthesized-lesson clusters into `propose()` calls; the twin-core version (§5 Phase 2) runs the proposer in a second profile. All required data (outcomes, eval trend, lineage archive) already exists.
- **CI** (G2). Nothing about self-improvement is trustworthy without an execution substrate for the fail-closed gate. `scripts/run_tests.sh` is already hermetic (`env -i`, `scripts/run_tests.sh:62-73`) — the workflow file is genuinely just missing.
- **File locking** (G4) and **red-team routing for all lesson sources** (G3) — small, local changes to `agent/lessons.py` / `agent/auto_mine.py`.
- **Governor scope** (G7) — widen FROZEN to pause lesson/mining/strengths writes; make promotion-path reads fail closed (the `can_promote` call sites already do).

### 3.2 Track 2 (core code) — go/no-go prerequisites

~~All currently ✗.~~ **As of 2026-07-31, prerequisite 1 is ✓ and the rest remain ✗.**
Ordered; 1–4 are hard blockers for Phase A, all seven for Phase B (§6):

1. ✓ **MET** — CI exists and is green on `main` (G2): `.github/workflows/tests.yml`,
   full suite hard-gated.
2. **Evaluator outside agent-writable paths.** Today eval specs live in `$JANUS_HOME/evals/` (`agent/evals.py:84-85`) — outside the `self_improve` allowlist but fully writable by the agent's ordinary file tools. They must move to a read-only mount or separate repo the proposing agent cannot touch. (DGM fabricated test logs the moment its harness was reachable — the codebase's own docstring cites this, `self_improve.py:5-8`.)
3. Sandbox-by-default for evaluating self-generated diffs (Docker/Modal/Daytona backends exist; flip the default for this path only).
4. Sealed held-out eval subset, rotated, never present in the proposing agent's context.
5. Governor + eval gate fail-closed on every promotion path (G7).
6. Repo invariants as executable tests: cache-prefix byte stability, config-loader agreement.
7. File-class policy that excludes the monoliths (`cli.py` 741 KB, `gateway/runner.py` 880 KB, `run_agent.py` 235 KB, `agent/conversation_loop.py` 280 KB) — the highest-blast-radius, least-reviewable surfaces.

### 3.3 Compatibility

No language/framework obstacle: Python 3.11 + pinned deps + uv lockfile is a good substrate for generated-code verification; profiles make isolated eval environments nearly free; the synchronous loop is irrelevant to Track 2 (proposals are offline). Cloud infrastructure is optional by design — CI (GitHub Actions) is the only new external dependency, and the sandbox backends already abstract remote execution. The research pattern being followed (non-parametric, harness-level evolution — DGM/SICA/ACE lineage) is exactly the one that works without training infrastructure; there is deliberately no fine-tuning loop (trajectory export exists, unconsumed).

---

## 4. Risk analysis *(component 3)*

### 4.1 Security

- **R1. Lesson-poisoning persistence (G3) — the top finding.** A prompt injection in any processed content can seed a reflexion or compression-sink lesson that bypasses the red-team gate, persists in `lessons.json`, and is then *pushed into every future similar turn* (`conversation_loop.py:828-840`). One-time poisoning of self-evolving stores is the highest-persistence attack class in the literature; Janus built the defense (dialectic gate) and routes only some sources through it.
- **R2. Skill-level code execution (G5, G6).** Agent-created skills are arbitrary Python executed at import, unscanned by default, on an unsandboxed host that later cron/kanban sessions run automatically.
- **R3. Kanban as an unscanned multi-writer channel (G8)** feeding auto-spawned headless workers, in contrast to cron's two-layer scanner.
- **R4. Track 2 additions:** eval-gaming (mitigated by prerequisite 2), agent-introduced dependencies (supply-chain policy must extend to generated diffs), provenance loss (mitigate: commit trailers + lineage), secrets exposure to proposer cores.

### 4.2 Stability

- **R5. Fail-open asymmetry inversion (G7).** The circuit breaker (governor) fails open and the default eval gate fails open; only `can_promote` fails closed. A corrupted `outcomes.json` (plausible via G4 races) silently disables the health assessment exactly when the loop is most active.
- **R6. Store corruption under concurrency (G4).** Automine background threads, sleep at exit, and gateway sessions can interleave `read → modify → write_text` on the same JSON files; last-writer-wins data loss is silent.
- **R7. Spend-control blind spots (G9):** unpriced models, in-flight work, and (Track 2) CI compute not in the ledger.
- **R8. Monolith blast radius:** the five largest files total ~2.3 MB of Python; any autonomous edit there is effectively unreviewable (hence prerequisite 7).

### 4.3 Maintainability

Three config loaders that can silently disagree; prompt-cache invariant enforced mostly by comments (partial tests only); stale planning docs (`plans/self-improvement-roadmap.md` marks shipped work "NOT STARTED") — dangerous specifically because *this* codebase's agent reads its own docs as ground truth; and no CI to hold any of it in place.

### 4.4 Twin-core-specific risks (why mutual approval is banned)

- **Collusion / mutual leniency:** two LLM cores judging each other converge on approving each other's output — the self-rewarding-LM collapse with extra steps. Rule: cores may *propose and critique*; only the deterministic gate stack + human may *approve*.
- **Correlated blind spots:** same base model → same failure modes; a cross-core review is not an independent check the way the eval suite is. Weight cross-core review as advisory, never as gate-satisfying evidence.
- **Cross-core poison propagation:** a poisoned lesson in core A becomes a proposal to core B; without G3 fixed first, twin-core *amplifies* the injection-persistence risk. Hence phase ordering: G3 closes before any proposer wiring.
- **Shared-store races:** cores must have disjoint `$JANUS_HOME`s (profiles guarantee this); the only shared artifact is the proposal archive, which must become append-only and locked (G4/G12).

### 4.5 Risk matrix

| Risk | Likelihood | Impact | Phase that closes it |
|---|---|---|---|
| R1 lesson poisoning | Medium | High | 1.1 |
| R2 unscanned skills on host | Medium | High | 1.3 |
| R5 fail-open inversion | Medium | High | 0.1 + 1.2 |
| R6 store races | High | Medium | 0.2 |
| R3 kanban injection | Low-Med | High | 1.4 |
| R7 spend blind spots | Medium | Medium | 1.3 |
| R4 eval-gaming (Track 2) | High (if unmitigated) | Critical | T2 prereqs 2-4 |
| Collusion (twin-core) | Medium | High | structural ban (§4.4) |
| R8 monolith edits (Track 2) | High (if allowed) | Critical | T2 prereq 7 |

---

## 5. Track 1 roadmap — wire & harden the artifact loop *(component 4a)*

Ordering principle: **safety harness before autonomy widening.** Never wire the proposer (Phase 2) while lessons bypass the red-team gate and the governor fails open.

### Phase 0 — Foundations (low risk, no behavior change)

| Item | Closes | Effort | Status (2026-07-31) |
|---|---|---|---|
| 0.1 Commit real CI: `scripts/run_tests.sh` + `janus evals gate --run` with `fail_closed=True` + `ruff`/`ty`, actions SHA-pinned per existing policy | G2, half of G7 | M | 🟡 **MOSTLY DONE** — `run_tests.sh` + `ruff` hard-gated; `evals gate --fail-closed` and `ty check` deliberately deferred (documented in the workflow header) |
| 0.2 File locking (e.g. `filelock`) + atomic write (the `feedback_signals.py:93-104` pattern) on all `learning/*.json` stores | G4 | S | ✅ **DONE** — `agent/store_lock.py` |
| 0.3 Fix `plans/self-improvement-roadmap.md` status header; mark `docs/agi-roadmap-2026.md` status table authoritative | G12 | S | ✅ **DONE** |
| 0.4 Cache-prefix byte-stability regression test (extend the existing cache-parity tests to full-prefix byte equality) | T2 prereq 6 | S | ⬜ **OPEN** |

### Phase 1 — Close the safety holes

| Item | Closes | Effort | Depends | Status (2026-07-31) |
|---|---|---|---|---|
| 1.1 Route reflexion + compression-sink lessons through `red_team_claims` (or a cheap heuristic screen + quarantine flag when the aux model is unavailable); tag lessons with `untrusted_content_in_context` provenance | G3 / R1 | M | — | ✅ **DONE** — `lessons.screen_lesson` |
| 1.2 Governor: fail closed on promotion-path reads; widen FROZEN to pause lesson writes, memory/skill mining, and model_strengths updates (advisory reads stay fail-open) | G7 / R5 | M | 0.1 | ✅ **DONE** — `learning_frozen()` + `regression_gate(fail_closed=True)` on the promotion path |
| 1.3 Flip `skills.guard_agent_created` → True for headless/cron/kanban sessions; conservative default price for unpriced models in the ledger | G5, G9 | S-M | — | 🟡 **HALF DONE** — the guard half shipped 2026-07-31 (see G5; the heuristic was dead code until the default became the `"auto"` sentinel). Unpriced-model pricing (G9) not started |
| 1.4 Kanban body/comment injection scan (reuse `_scan_assembled_cron_prompt`); hash-chained append-only autonomy audit log (freeze/unfreeze, promotions, spawns) | G8, G12 | M | — | ✅ **DONE** — `_scan_kanban_task_for_injection` + `agent/audit_log.py` |

### Phase 2 — Wire the loop (the headline, twin-core pattern)

| Item | Closes | Effort | Depends | Status (2026-07-31) |
|---|---|---|---|---|
| 2.1 **Proposer v1 (single-core):** a sleep-cycle step that converts eval-trend regressions + synthesized-lesson clusters into `self_improve.propose()` calls, evaluates variants in an isolated profile (`get_janus_home` override), and leaves them for `janus self-improve` review. Human approval stays ON. | G1 | M | 1.1, 1.2 | ✅ **DONE** — `agent/proposer.py` + `agent/eval_orchestrator.py`, `sleep.py:381` |
| 2.2 **Proposer v2 (twin-core):** second profile ("core B") runs the proposer/red-team against core A's stores and vice versa — proposer≠approver by construction; cross-core critique attached to each proposal as advisory evidence. Disjoint homes; shared archive append-only. | G1 + governance separation | M-L | 2.1 | ✅ **DONE** — `agent/twin_review.py` (veto-only, `sleep.py:405`), `learning.self_improve.twin_review` default off |
| 2.3 Shaped reward into promotion: `assess_promotability` consumes `skill_reward_trajectory` alongside the boolean | G10 | S-M | 2.1 | ✅ **DONE 2026-08-01** — both bars must clear; governor tightens both under CAUTION |
| 2.4 Safe default-on: fold `janus learning enable`'s read-only bundle into first-run setup (write actions stay opt-in) | G11 | S | 1.1 | ✅ **DONE** — `janus_cli/learning_onboarding.py` |

### Phase 3 — Compounding (deferrable)

3.1 Persistent vector index (sqlite-vec sidecar) + per-fact provenance → closes the paraphrase-blind-spot and gives the proposer better evidence. 3.2 Trajectory-export consumer (eval-seed generation, not fine-tuning). Both effort L.

---

## 6. Track 2 feasibility — core-code self-modification *(component 4b)*

**Verdict: conditional go.** Feasible on this codebase's raw material; irresponsible before the §3.2 prerequisites are met. Recommended phasing, twin-core roles throughout:

- **Phase A — agent-as-contributor.** Contributor core proposes core-code changes as PRs from an isolated profile + git worktree, each with a failing-test reproduction; gatekeeper core red-teams the diff (advisory); CI runs the full hermetic suite + fail-closed eval gate; **a human merges everything.** Scope: bug fixes and test additions only. *Entry:* prereqs 1–4. *Exit:* ≥20 merged PRs, <10% revert rate over 60 days.
- **Phase B — auto-merge for narrow file classes.** Tests, docs, plugin manifests, prompt templates — behind the full gate stack plus a **CI-enforced** path allowlist (enforced outside the agent, not agent-side). *Entry:* all 7 prereqs + Phase A exit. *Exit:* zero gate escapes in 60 days.
- **Phase C — guarded core paths.** Small isolated modules (individual `tools/*.py`, single-file plugins) with canary-profile staged rollout and a rehearsed rollback (checkpoint_manager + git revert drill). Monoliths remain excluded.
- **Phase D — loop closure (aspirational).** The agent edits its own proposer/evaluator *scaffolding* — with the evaluator still externally owned and human approval permanent for this class. Requires its own go/no-go review; do not schedule it now.

**Hard invariants — never crossed at any phase:**

1. The agent never writes: eval definitions, CI workflows, `self_improvement_governor.py`, `autonomy_guard.py`, `self_improve.py` gate logic, or the audit stream.
2. `require_human_approval` must not be flippable by any autonomous path (today it is ordinary config — move it out-of-band or require the sentinel-file pattern for changes).
3. `AUTONOMY_FROZEN` honored by every promotion path including Track 2 CI jobs.
4. Rollback is a tested, single-command operation demonstrated before each phase advance.
5. Track 2 spend (including CI compute) accrues to the same rolling caps.

---

## 7. Success metrics *(component 5)*

| Metric | Source | Baseline | Target | Cadence | Agent-influenceable? |
|---|---|---|---|---|---|
| Eval pass-rate trend (pass^k, noise-floored) | `eval_trend.py` trend.jsonl | current curve | monotone, no regression-gate trips | per sleep/CI run | Partly — held-out subset is the check |
| Time-horizon KPI (longest ≥50% bucket) | `eval_trend.py:97-165` | current | +1 bucket / quarter | weekly | Partly |
| Lesson efficacy ratio (helpful:harmful) | `lessons.json` counters | — | >3:1, harmful trending ↓ | weekly | Yes — never a promotion criterion alone |
| Friction score (interrupt/steer/denial) | `feedback_signals.py` | — | trending ↓ at equal task volume | weekly | **No (by design — observability only)** |
| $ per successful task | `cost_ledger.jsonl` ÷ outcomes | — | flat or ↓ while pass-rate ↑ (HAL doctrine) | monthly | Partly |
| Governor OK duty-cycle | governor state log | — | >95% OK, zero unexplained FROZEN | monthly | No |
| **Proposal funnel** | `self_improve.json` lineage | 0 (nothing proposed yet) | propose→promote conversion 10–30%; survive-30-days >90% | monthly | Yes — human approval is the check |
| **Revert/rollback rate** | lineage + git | — | <10% (Phase A exit criterion) | per phase | No |
| **Singularity metric** (% merged diff lines agent-authored) | git trailers/blame | ~0 for core code | reported honestly per release, no target | per release | No |

Anti-gaming rule: any agent-influenceable metric is reported but never *solely* gates promotion; the fail-closed eval suite + human approval remain the deciding pair.

---

## 8. Governance & compliance *(component 6)*

Mapped to standard control families rather than invented frameworks:

- **NIST AI RMF:** Map = this document's audit + gap register; Measure = eval spine + governor + learning metrics; Manage = autonomy_guard (kill switch, caps) + phased rollout; Govern = human approval gate, lineage archive, the audit stream (Phase 1.4), and this report's review cadence.
- **Change management (ISO 27001 A.8.32 / SOC 2 CC8 analog):** every self-modification is a change record (proposal id, diff, eval evidence, approver, backup) — the lineage archive already implements this for artifacts; Track 2 inherits it via PRs. Twin-core delivers proposer/approver separation of duties; today's single-core design has none.
- **Least privilege:** the kind/path allowlists + `O_NOFOLLOW` writes (`self_improve.py:38,42-46,87-108`) are a model to extend — the same pattern should protect the eval dir and governor state (Track 2 invariant 1).
- **Kill-switch & incident response:** publish a freeze-scope table (what `AUTONOMY_FROZEN` stops — new cron/kanban/delegate/promotions — vs. what continues — in-flight sessions, interactive use); drill `janus autonomy freeze` → verify → unfreeze quarterly; treat a governor FROZEN plus any promoted-artifact regression as an incident requiring lineage-based rollback.
- **EU AI Act posture** (informational, not legal advice): a local-first, operator-controlled developer tool with human-approved self-modification sits well below high-risk categories, but *distributing* a default-on autonomous self-modifying configuration would warrant a systemic-risk-style review; keep write actions opt-in in distributed defaults.
- **Governance gaps to close** (all appear in the roadmap): fail-open governor (1.2), config-flippable human approval (T2 invariant 2), no tamper-evident audit (1.4), no dual control on gate-file changes (T2 invariant 1 + CODEOWNERS once CI exists).

---

## 9. Testing protocols for self-generated changes *(component 7)*

**Existing substrate (strong):** hermetic per-file subprocess test isolation (`scripts/run_tests.sh:62-73`, ~1,459 test files); pass^k evals with capability/regression kinds and a noise floor; dialectic red-team gate; skill self-tests; checkpoint-manager rollback; invariant-style-tests-only review policy.

**The protocol ladder every self-generated change must climb (additions in bold):**

1. **CI as substrate** (Phase 0.1) — hermetic suite + `evals gate --run` fail-closed on every proposal evaluation.
2. Isolated-profile evaluation — variants applied and scored only in a throwaway `$JANUS_HOME` (already the `record_evaluation` contract, `self_improve.py:261-266`).
3. **Sealed held-out evals** — a rotating subset excluded from the proposer's context and from anything the loop optimizes against; refreshed quarterly.
4. **Byte-stability golden test** — cache-prefix equality across mocked multi-turn sessions (Phase 0.4).
5. **Adversarial cross-core review** — the gatekeeper core attempts to *refute* each proposal (find the regression, the injection, the gamed metric) before human review; advisory, attached to the proposal record.
6. **Canary staged promotion** — promote to a canary profile first; N clean sessions (no friction spike, no eval drop) before the default profile.
7. **Mutation/property tests for the gate files themselves** (`self_improve.py`, `self_improvement_governor.py`, `autonomy_guard.py`) — these ~700 lines guard everything else; they deserve the strongest tests in the repo.
8. **Scheduled rollback rehearsal** — a cron job that promotes a no-op variant to canary, rolls it back, and alerts on any failure; rollback must never be tested for the first time during an incident.

---

## 10. Appendices

### Appendix A — Verification log (this pass, 2026-07-10, at `8857e06`)

> **⚠️ This log records what was true at `8857e06` on 2026-07-10.** Six rows have since been
> invalidated by shipped work and are marked **[SUPERSEDED]** — they are *not* current
> findings. A re-verification pass on 2026-07-31 confirmed the rest still hold.

| Claim | Evidence | Verdict |
|---|---|---|
| `propose()` has no autonomous callers | grep: only `janus_cli/main.py:15714-15792` (CLI review) + `janus_cli/config.py:1839-1850` (defaults); `sleep.py:294`/`skill_graph.py:324` import the governor, not self_improve | **[SUPERSEDED 2026-07-31]** — `agent/proposer.py` now calls it from `sleep.py:381` |
| No CI workflows exist | `.github/` absent; `git ls-files | grep .github` → 0; dangling refs `scripts/run_tests_parallel.py:59-61` | **[SUPERSEDED 2026-07-31]** — `.github/workflows/tests.yml` hard-gates the full suite |
| Governor fails open, narrow scope | `self_improvement_governor.py:15-20` (design contract), `:180-182` (except → OK); consumers limited to promotion paths | **Confirmed** |
| Promotion gate = 6 fail-closed conditions incl. human approval default-True | `self_improve.py:300-345`, `:80-82`, `:276` | **Confirmed** |
| Path/kind allowlist + O_NOFOLLOW, never core code | `self_improve.py:38,42-46,87-108,10-13` | **Confirmed** |
| autonomy_guard enforced at cron/kanban/delegate/self-improve | `delegate_tool.py:2016`, `cron/scheduler.py:2042`, `kanban_db.py:6106-6111`, `self_improve.py:334` | **Confirmed** |
| Guard gates new work only; unpriced = $0; blocked_reason fails open; sentinel is reliable layer | `autonomy_guard.py:17-28,119-120,39` | **Confirmed (documented honestly in-module)** |
| Reflexion + compression lessons bypass red-team | red_team callers only `playbook.py:229-236`, `sleep.py:186-237`; writers `auto_mine.py:104-110`, `conversation_compression.py:271` | **[SUPERSEDED 2026-07-31]** — both writers now call `lessons.screen_lesson` |
| Per-turn lesson push injection, cache-safe | `conversation_loop.py:828-840,1030-1045`; `lessons.py:426-434` | **Confirmed** |
| Learning stores unlocked/non-atomic (except feedback_signals) | `lessons.py:83-86,144-151`, `outcome_tracker.py:50-53`, `model_strengths.py:89` vs `feedback_signals.py:93-104` | **[SUPERSEDED 2026-07-31]** — all three now use `agent/store_lock.py` (flock + atomic replace) |
| `guard_agent_created` default False | `skill_manager_tool.py:59-60` | **[SUPERSEDED 2026-07-31]** — the default is now the `"auto"` sentinel: on in headless/cron, off in interactive/gateway. See G5 for why a boolean default could not work here |
| Eval specs agent-writable; gate default fail-open | `evals.py:84-85`; `eval_trend.py:347-359,407-408` | **Confirmed** |
| Sleep GRADUATE/PROMOTE gating | `sleep.py:253-299` | **Confirmed** |
| Shaped reward unconsumed by promotion | `outcome_tracker.py:124,133`; `skill_graph.py:252,268` | **Confirmed** |
| `learning enable` flips read-only bundle only | `janus_cli/main.py:15500-15540` | **Confirmed** |
| Monolith sizes | `wc -c`: cli.py 741,426; gateway/runner.py 880,154; conversation_loop.py 279,546; run_agent.py 235,336; janus_state.py 187,421 | **Confirmed** |
| Kanban bodies unscanned (cron is) | scanner only in `cron/scheduler.py:1220,1294-1297` | **[SUPERSEDED 2026-07-31]** — `kanban_db.py:6035` scans + blocks at claim time |
| Cache invariant "comment-only" | **Revised:** partial tests exist (`tests/run_agent/test_background_review_cache_parity.py`, `tests/agent/test_system_prompt_restore.py`); full byte-stability test still absent | **Partially confirmed** |
| All 10 moves shipped | `git log 4ec212d..498bc55` — 9 commits covering moves 1-10 (1+5 combined in `0b3f357`) | **Confirmed** |

### Appendix B — Shipped-moves ledger

| Move | Commit | Move | Commit |
|---|---|---|---|
| 1+5 Push recall + live sleep | `0b3f357` | 6 Learned routing (+taxonomy/cost) | `fa48a32`, `7257d97` |
| 2 Measurement spine | `dd21a42` | 7 Hybrid RRF retrieval | `005bfd4` |
| 3 Autonomy safety floor | `7fdc5e1` | 8 Time-horizon KPI | `f30f0d1` |
| 4 Lesson efficacy | `d6abe18` | 9 DGM-lite self-modification | `1c4a128` |
| | | 10 Implicit-feedback sensors | `498bc55` |

### Appendix C — Glossary

**Governor states:** OK (normal), CAUTION (tightened promotion thresholds), FROZEN (autonomous promotion paused). **The gate stack:** the six `can_promote` conditions (§2.4). **Twin-core:** two isolated Janus profiles proposing/critiquing for each other; deterministic gates + human approve. **Kinds:** `skill | prompt_fragment | lesson_policy` — the only self-modifiable artifact classes. **Fail-open vs fail-closed:** advisory health reads default open (a broken sensor must not wedge the agent); promotion decisions default closed (uncertainty must not modify the agent). **Singularity metric:** fraction of merged diff lines authored by the agent itself.
