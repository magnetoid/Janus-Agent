# Token-Optimization Audit (Janus platform)

Generated 2026-06-29 from a 10-dimension subagent audit (53 findings) with adversarial
verification against the **prompt-caching invariant** (never alter past context, change the
toolset mid-conversation, or rebuild the system prompt mid-conversation; the only sanctioned
context mutation is compression).

Findings are deduplicated and ranked by **impact × safety**. Token estimates are the
finders' rough order-of-magnitude figures, not measured.

## How to read impact

- **Static system-prompt / tool-schema text** lives in the *cached* prefix. Its marginal
  per-request cost is low (cached after the first write); the real cost is (a) the one-time
  cache-write per session and (b) re-send on every cache **miss**. So prompt/schema trims are
  most valuable on high-session-count surfaces (gateway: fresh `AIAgent` per message).
- **Per-turn injected text after the cache breakpoint** (memory context, ephemeral prompt
  additions) is paid on *every* call — higher leverage.
- **Auxiliary / background LLM calls** (compression, mining, dialectic, review) are pure
  token multipliers off the main loop.

---

## Tier 1 — Highest impact, caching-core (each its own careful change + tests)

| # | Opportunity | File(s) | Est. impact | Risk |
|---|---|---|---|---|
| T1 | **Cache the Anthropic tools array.** `convert_tools_to_anthropic` forwards `cache_control` but nothing ever sets it, so the (large, session-stable) tools schema is re-sent uncached on every native-Anthropic request. | `agent/anthropic_adapter.py` (1462–1476, 2138–2211), `agent/prompt_caching.py` | ~8–25K input tok/call after turn 1 on the `anthropic_messages` path | **Budget catch:** `apply_anthropic_cache_control` already uses all 4 breakpoints (system + last 3 msgs). Adding a tools breakpoint = 5 → API 400. Must drop to **tools + system + last 2 msgs = 4**. Cache-safe once coordinated. |
| T2 | **Discord voice-channel context busts the system-prompt cache every turn.** `vc_context` is appended to the ephemeral system prompt, which changes each message → full cache-write every turn. | `gateway/runner.py` (7689–7695, ~16096) | ~3–6K tok cache-write/turn for Discord voice | Low — moving volatile content into the user message *improves* caching |
| T3 | **Background review forks get the full conversation history** (40–100K tok) unfiltered. | `agent/background_review.py` (474–482) | 40–100K tok/review on tool-heavy sessions | Low-Med — filter snapshot (strip old tool bodies); opt-in feature |

## Tier 2 — High/medium impact, cache-safe (implement now)

| # | Opportunity | File(s) | Est. impact |
|---|---|---|---|
| [27] | **Don't inherit parent `prefill_messages` into subagents.** | `tools/delegate_tool.py:1151` | 500–2000 tok × N subagents |
| [5] | **Compress 4 verbose cronjob param descriptions** (`no_agent`/`deliver`/`profile`/`workdir`). | `tools/cronjob_tools.py:797–840` | ~590 tok/session (gated to gateway/interactive) |
| [25] | **Sleep cycle re-mines sessions already mined at session-end.** Track mined IDs in `sleep_state.json`. | `agent/sleep.py:213–227`, `agent/auto_mine.py` | ~30–40K tok/sleep-cycle |
| [21] | **`red_team_claims` sends the full 12K-char transcript to both advocate and skeptic.** Cap to ~3K. | `agent/deliberation.py:252–308` | ~4500 tok/dialectic mine (opt-in) |
| [52] | **Gateway delivery-options block injected for every session** regardless of home-channel/cron. Gate it. | `gateway/session.py:393–419` | ~100 tok/session → ~50K/day at 500 sessions |
| [28] | **Subagent goal text duplicated** in the child system prompt *and* as the user message. | `tools/delegate_tool.py:620–623, 1557–1560` | 150–900 tok/delegation batch |
| [6]/[29] | **`_TOOLSET_LIST_STR` (34 names) embedded twice** in `delegate_task` schema. Keep one copy. | `tools/delegate_tool.py:2757–2778` | ~100–212 tok/turn when delegation active |
| [18] | **Iterative compression sends the full previous summary** (≤25.6K chars) verbatim to the aux LLM. Truncate section-aware. | `agent/context_compressor.py:1408–1422` | ~4400 tok/update |
| [22]/[47] | **`_SKILL_REVIEW_PROMPT` (~1465 tok) sent every background review;** trim duplicated "do NOT" lists. | `agent/background_review.py:45–148` | ~715 tok × review iterations |
| [30] | **`delegate_task` top-level description (~776 tok)** verbose WHEN-TO/IMPORTANT blocks. | `tools/delegate_tool.py:2592–2643` | ~200–350 tok/turn |
| [10] | **`delegate_task` `acp_command`/`acp_args` descriptions** over-explain a rare transport. | `tools/delegate_tool.py:2811–2835` | ~200 tok/session |

**Status (2026-10-30):**
- **Done before this check:**
  - [27]: `prefill_messages=None` for subagents.
  - [28]: the goal appears only as the user message.
  - [6]/[29]: one `_TOOLSET_LIST_STR`.
  - [30]: the `delegate_task` description is now 153 chars.
  - [25]: sleep mines only since the previous cycle's `last_run` and skips child sessions.
- **Done now:** [5]. The four cronjob param descriptions were trimmed, taking the schema from 7,560 to 6,484 chars.
- **Declined:**
  - [22]: the skill-review prompt shapes learning behaviour, so trimming it is a behaviour change.
  - [18]: truncating the previous summary loses the only record of earlier turns.
- **Left open:**
  - [52]: gating needs the session's toolsets plumbed into `build_session_context_prompt`; about 100 tok.
  - [21]: red-team is opt-in.
  - [10]: `acp_command` is 309 chars.

## Tier 3 — Cache-safe trims (low individual, batchable; best for gateway)

System-prompt (all set once at session start → cache-safe):
- [31] Trim `OPENAI_MODEL_EXECUTION_GUIDANCE` redundant `<tool_persistence>`/`<act_dont_ask>` — `agent/prompt_builder.py:315–373` (~170 tok, gpt/codex/grok).
- [32] Drop `skill_view(name='janus-agent')` clause from `JANUS_AGENT_HELP_GUIDANCE` (skills_prompt already covers it) — `agent/prompt_builder.py:132–141` (~93 tok).
- [33] Shorten default-profile hint to one line — `agent/system_prompt.py:274–294` (~72 tok).
- [34]/[42] Condense `MEMORY_GUIDANCE` "do NOT save" enumeration — `agent/prompt_builder.py:143–164` (~50–107 tok).
- [35] Merge duplicate "load the skill even if…" sentences — `agent/prompt_builder.py:1280–1307` (~60 tok).

Tool schemas (cache-safe):
- [9] Remove "Requires browser_navigate" boilerplate from 9 browser tools — `tools/browser_tool.py:1489–1614` (~108 tok).
- [8] Single-line the `execute_code` embedded tool signatures — `tools/code_execution_tool.py:1698–1810` (~118 tok).
- [2] Cap MCP tool descriptions (~512 chars) — `tools/mcp_tool.py:3125–3143` (server-dependent, can be 1000s of tok).
- [3] Remove `deferred_count` from `tool_search` bridge description (enables cross-session tool-cache reuse) — `tools/tool_search.py:426–455`.
- [4] Drop Examples block from `messages_send` MCP docstring — `mcp_serve.py:738–752` (~33 tok).
- [7] Drop nesting note from `delegate_task` role param — `tools/delegate_tool.py:2660–2715` (~37 tok).

Memory injection (per-turn caps — protective):
- [36] Shorten `<memory-context>` fence note — `agent/memory_manager.py:227–241` (~35 tok/call).
- [37] Add a Janus-level prefetch size cap (~1500–2000 tok) — `agent/conversation_loop.py:996–1006`.
- [39] Supermemory: skip static-profile inject when MEMORY.md/USER.md non-empty — `plugins/memory/supermemory/__init__.py:567–582`.
- [40] RetainDB: combined size cap (~500 tok) across 3 streams — `plugins/memory/retaindb/__init__.py:542–623`.
- [41] ByteRover: cap raw CLI stdout — `plugins/memory/byterover/__init__.py:215–231`.
- [38] Drop `[pct% — N/M chars]` from MEMORY/USER block headers — `tools/memory_tool.py:610–626` (~20 tok).

Learning-loop (opt-in flags, low default impact):
- [23] Batch `reconcile_candidate` across mined facts — `agent/memory_miner.py:209–246`.
- [24] Reduce transcript to two `quorum_classify` judges — `agent/outcome_tracker.py:345–395`.
- [26] Lower `red_team` advocate/skeptic `max_tokens` — `agent/deliberation.py:280–307`.
- [49] Strip synthetic length-continuation messages once the turn completes — `agent/conversation_loop.py:1841–1851`.

## Tier 4 — Needs design (real, but risk/behavioral — decide before implementing)

- [11] **Extend `tool_search` deferral to rarely-used "core" tools.** ~425 tok off the always-visible schema, but changing the active tool list interacts with T1 (caching the tools array) and risks mid-session toolset change. `toolsets.py:31–78`.
- [17] **Proactive tool-result pruning before the 50% threshold.** High value on tool-heavy sessions but `cache_risk=high` on Anthropic (mutating past context). Safe only when gated to non-cached providers. `agent/context_compressor.py:788–954`.
- [45] **Lower `DEFAULT_RESULT_SIZE_CHARS` 100K→30–40K** so large outputs persist+preview instead of riding inline. Behavioral — could spill data the agent needs inline. `tools/budget_config.py:15–19`.
- [48] **Add a cache breakpoint after the last stable tool turn.** `cache_risk=high`; interacts with the 4-breakpoint budget and T1. `agent/prompt_caching.py:49–79`.
- [19] **Rewrite `SUMMARY_PREFIX` 344→~90 tok.** Load-bearing anti-injection directive — trim carefully, behavior-test. `agent/context_compressor.py:37–61`.

## Out of scope (latency/CPU, not tokens)

- [44] deepcopy→shallow copy in `apply_anthropic_cache_control` (CPU; `cache_risk` if done wrong).
- [46] Incremental `api_messages` rebuild (CPU; cache/correctness risk).
- [51] Per-turn `config.yaml` read bypasses mtime cache (latency) — `gateway/core.py:884–911`.
- [53] Triple `config.yaml` deepcopy per message turn (CPU) — `gateway/runner.py:7194,7344,15388`.

## Rejected (already optimal)

- [15] Gateway `_restore_or_build_system_prompt` — already guards cache-miss recovery correctly.
- [16] Background-review fork already inherits cached system prompt + matched toolset (reference impl, ~26% measured reduction).
