# Janus ← OpenClaw / Hermes Agent: gap analysis and implementation plan

## Context

Janus is a rebranded fork of Nous Research's **Hermes Agent**, snapshotted on 2026-06-08 (commit `0a873d7`, roughly Hermes v0.17/v0.18). Since then upstream shipped v0.19 → v0.22.5 (July–October 2026, ~300k insertions), and **OpenClaw** shipped 2.0 (2026.8.1) through 2026.10.8. Janus meanwhile built its own layer upstream does not have: the governed learning loop (sleep, dialectic gate, governor, playbook, lessons), Kanban, complexity-based model routing, AG-UI + `/v1/config` + Blob adapter, security posture audit, Docker-by-default for unattended runs, cron memory, child steer, outbound webhooks, importers.

Decision taken with the user: **cherry-pick and re-implement** selected upstream features natively (no git re-sync with Hermes), as a **balanced backlog** ranked by usefulness per effort. Everything ships default-off behind `config.yaml` keys unless noted, per the repo's invariants (cache-safe, `get_janus_home()`, no change-detector tests, tests via `scripts/run_tests.sh`).

Sources: Hermes release notes v0.19–v0.22.5 (`gh release view` on NousResearch/hermes-agent), OpenClaw release notes 2026.8.1 / 9.6 / 10.1 / 10.5 and docs (memory, sandboxing, code-mode). Janus status verified by grep/read of the tree on 2026-10-28.

---

## 1. Where Janus already stands (skip these — already shipped or equivalent)

| Upstream feature | Janus equivalent |
|---|---|
| Hermes 0.21 cron memory, steer subagents, 0.20 outbound webhooks | `4f311d0` (cron memory, child steer, outbound webhooks) |
| Hermes 0.22 `import-agent` (Claude Code/Codex/Cursor/OpenClaw) | `janus_cli/agent_import.py`, `janus claw migrate` |
| Hermes 0.19 Bitwarden / 1Password secret sources | `agent/secret_sources/` |
| Hermes 0.19 smart approvals | `tools/approval.py` mode `smart` (not yet the default — see 2.6) |
| OpenClaw sandboxing (off by default) | `terminal.headless_backend: auto` → Docker for cron/kanban/one-shots (`8807b0b`) — stronger default than OpenClaw |
| OpenClaw daily notes, MEMORY.md/USER.md, hybrid search, dreaming | `73581a3` daily snapshots, `tools/memory_tool.py`, RRF retrieval (`005bfd4`), `agent/sleep.py` |
| Hermes programmatic tool use / OpenClaw Code Mode | `execute_code` RPC tool already in tree (30 refs) — QuickJS variant not needed |
| Hermes 0.21 oversized tool results spill, lean-tail compression | `tools/tool_result_storage.py`, `agent/context_compressor.py` |
| Hermes Honcho user modeling, session FTS5 search | `plugins/memory/honcho`, `janus_state.py` |
| OpenClaw installed-skill discovery (lazy load) | `tools/tool_search.py`, skills loaded on demand by `tools/skills_tool.py` |
| OpenClaw 9-tool MCP channel bridge | `mcp_serve.py` (explicitly ports it, adds `channels_list`) |
| OpenClaw cron/webhooks/API triggers | `cron/`, `janus webhook subscribe` |

---

## 2. Tier 1 — do first (high value, S–M effort)

### 2.1 Memory flush before compaction (OpenClaw memory-core)
**What:** Before `context_compressor` summarises, run one silent, tool-restricted turn on a *private copy* of the conversation asking the model to persist anything durable (facts → MEMORY.md/USER.md, working notes → today's daily journal). Append-only; reject duplicate text; never blocks compaction on failure.
**Why:** The AGI audit notes consolidation "only fires at exit". Compaction is the moment context is lost; this is the cheapest way to stop forgetting mid-session.
**Where:** `agent/conversation_compression.py` (~line 431, where `MemoryProvider.on_pre_compress` is called) → new `agent/memory_flush.py`; writes via existing `tools/memory_tool.py` and the daily-journal writer from `73581a3`. Config `memory.flush_before_compaction: false` in `janus_cli/config.py` `DEFAULT_CONFIG`. Spend goes through `agent/cost_ledger.py`.
**Cache-safety:** runs at the compression boundary only; the flush turn is discarded, not appended to the live transcript.
**Tests:** `tests/agent/test_memory_flush.py` — flush invoked once per compaction, failure does not block compaction, replayed content not appended twice.

### 2.2 Delivery-obligation ledger (Hermes 0.19) + inbound survival (OpenClaw 2.0)
**What:** A gateway-side durable record: when a turn produces a final reply (or cron/kanban/delegate result) targeted at a platform, write an *obligation* row to `state.db` before sending; mark delivered on ack; on gateway start, replay undelivered rows with backoff. Symmetric: accepted inbound messages are journaled before processing so a restart mid-turn re-queues them.
**Why:** Janus has "state.db resilience" but no obligation concept (grep: 0 hits). Lost replies after restarts/provider stalls are the most visible reliability failure in a gateway agent.
**Where:** new table in `janus_state.py` (`SessionDB`), new `gateway/delivery_ledger.py`, hooked where `gateway/runner.py` calls adapter `send`/deliver and in `cron/scheduler.py` delivery. Reuse `agent/outbound_webhooks.py` retry pattern. CLI: `janus gateway obligations [--replay]`. Config `gateway.delivery_ledger: true` (default **on** — it only adds durability).
**Tests:** `tests/gateway/test_delivery_ledger.py` — row written before send, cleared on success, replayed on start, idempotent (no duplicate sends).

### 2.3 Durable background delegation with live transcripts (Hermes 0.19)
**What:** `delegate_task(background=true)` returns a handle immediately; the child runs in a worker (thread/subprocess reusing the existing Kanban worker spawn path), its transcript streams to a per-child session in `SessionDB`, and completion delivers the result back into the parent session (CLI/TUI notice, or platform message via 2.2). `janus delegations list|attach|stop`. Survives gateway restart via a durable record.
**Why:** `tools/delegate_tool.py` is sync-blocking (flagship gap #2; AGI doc calls full async a trade-off). Background delegation is the pragmatic 80% without rewriting `conversation_loop.py`.
**Where:** `tools/delegate_tool.py` (+ `background` param, keep existing leaf/orchestrator roles and depth-2 limit), `janus_cli/kanban_db.py` worker spawn for the process model, `agent/autonomy_guard.py` so background spawns count against spend caps, steer via the existing child-steer path. Config `delegation.background.enabled: false`.
**Tests:** handle returned without blocking, result delivered to parent, spend cap enforced, parent interrupt stops children.

### 2.4 30-day usage & cost report (OpenClaw 9.6)
**What:** `janus usage [--days 30] [--by session|platform|model|job]` reading `cost_ledger.jsonl`: tokens, estimated USD, session counts, top skills/tools. Also surface in `/usage` slash command and the web dashboard.
**Why:** Flagship doc names token/cost economics as the biggest blind spot; AGI audit says the ledger is "write-only in production". Zero new data collection — just a reader.
**Where:** `agent/cost_ledger.py` (`load_ledger`, `session_total_usd` exist, consumed only by tests), new `janus_cli/usage_report.py`, `CommandDef` in `janus_cli/commands.py`. Mark unpriced models explicitly ("n/a", not $0).
**Tests:** aggregation invariants (sum of per-model == total; unknown price → flagged, not zero).

### 2.5 Skill version history + undo (OpenClaw Skill Workshop 2.0)
**What:** Every write by `skill_manage`, the skill miner, curator, or `self_improve` to a skill keeps up to N=10 prior versions; `janus skills history <skill>`, `diff`, `undo`, `archive/restore`.
**Why:** Janus already snapshots before mutation (`agent/curator_backup.py`, `skill_graph.auto_promote_drafts`) but there is no user-facing history or undo; this is the trust surface for an agent that edits its own skills.
**Where:** generalise `agent/curator_backup.py` into `agent/skill_versions.py` (bounded, archive-not-delete); call it from `tools/skill_manager_tool.py`, `agent/skill_miner.py`, `agent/self_improve.py`; CLI in `janus_cli`. Storage under `get_janus_home()/skills/.versions/<skill>/`.
**Tests:** cap honoured, undo restores byte-exact, pinned skills respected, never deletes.

### 2.6 Approval hardening: scoped approvals + smart-by-default (OpenClaw 2.0, Hermes 0.19)
**What:** (a) Bind each approval to (command hash, session, approver identity, request id) so a gateway "yes" from one person/chat cannot satisfy another session's prompt; (b) flip `approval.mode` default to `smart` for interactive sessions, keep `manual` for headless unless sandboxed; (c) have `janus security audit` report the effective mode.
**Why:** Janus' approval gate is strong on patterns but approvals are per-session, not per-principal — matters once multiple chats/profiles share a gateway.
**Where:** `tools/approval.py` (approval record shape), `gateway/runner.py` approval handlers (the double-message-guard pitfall in AGENTS.md applies), `janus_cli/security_posture.py`.
**Tests:** approval from wrong session/principal rejected; default-mode matrix (interactive vs headless vs sandboxed).

---

## 3. Tier 2 — next (clear value, M effort)

### 3.1 Platform-native choice pickers for `clarify` (Hermes 0.19/0.21, 0.22.5)
Render `clarify` options as Telegram inline keyboards / Slack blocks / Discord buttons instead of numbered text; also let plugin slash commands present a choice. Extend `clarify` to several independent questions per call (Hermes 0.21) if `agent/…clarify` does not already. **Where:** `gateway/platforms/base.py` capability flag `supports_choices`, per-adapter render + callback mapping, `tools/` clarify tool. Fall back to text where unsupported.

### 3.2 Delegation profiles / pluggable delegation backends (Hermes 0.22.5)
Named subagent profiles in `config.yaml` (`delegation.profiles.<name>: {provider, model, toolsets, max_iterations, budget_usd}`) selectable from `delegate_task(profile=…)`, with routing from `agent/model_routing.py` as the default when none given. A guided picker in `janus delegation profiles`. Complements 2.3.

### 3.3 Encrypted external backups (OpenClaw 10.1)
`janus backup create|restore|list --to <dir|s3://…>` of `janus_home` (state.db via SQLite backup API, memory, skills, config without `.env`), optional age/GPG encryption, cron-schedulable via the existing scheduler, retention N. **Where:** new `janus_cli/backup.py`; register a built-in cron job type in `cron/jobs.py`.

### 3.4 Memory provenance + lint (OpenClaw Memory Wiki, scaled down)
Give MEMORY.md/journal entries structured provenance (source session id, timestamp, confidence, `supersedes`, `untrusted_context` flag) stored in a sidecar table, and `janus memory lint` reporting contradictions/stale facts using the existing sleep RECONCILE logic. No separate wiki vault. **Where:** `agent/memory_recall.py`, `agent/sleep.py`, `agent/memory_miner.py`; `agent/untrusted_content.py` sets the flag.

### 3.5 Local models in one command (Hermes 0.22)
`janus model --local`: detect hardware (RAM/VRAM/Apple Silicon), install/update llama.cpp (or use Ollama if present), pick the best-fitting GGUF from a small curated table, register it as a provider profile. Complements the frictionless first run (`de70a61`) with a no-API-key path. **Where:** `janus_cli/model_switch.py`, new `janus_cli/local_models.py`, existing llama.cpp transport references.

### 3.6 Plugin lifecycle hooks: `post_compaction`, MCP progress (Hermes 0.22.3)
Add `on_post_compress` to `MemoryProvider` and the general plugin ctx; forward MCP `notifications/progress` from `tools/mcp_tool.py` into the tool-progress display (CLI spinner text, TUI tool row, gateway typing). Small, unblocks plugin authors.

### 3.7 A2A v1.0 server (Hermes 0.20)
Expose Janus as an A2A agent (agent card + task endpoints) on `gateway/platforms/api_server.py` next to the OpenAI-compatible and AG-UI routes, reusing their auth. Lets Blob and other agents call Janus without bespoke adapters. Bot-to-bot DMs (`hermes peer`) are **not** recommended — Kanban already covers intra-Janus coordination.

---

## 4. Tier 3 — later / opportunistic

- **Streaming voice with barge-in + wake words; live transcription in CLI/TUI** (Hermes 0.20/0.21.6). Large; only `plugins/google_meet` has wake-word code today. Do after Tier 1–2.
- **Multi-profile single gateway** (Hermes 0.19/0.21 profile routing). Janus profiles are whole instances; serving several from one process touches `gateway/runner.py` (18.5k LOC). Defer.
- **Release channels** `janus update --channel stable|main` + signed packages (Hermes 0.21.7/0.22).
- **Data-training-tier warnings** per provider in `janus model` (Hermes 0.21). Small; privacy signal.
- **Grounded citations / fact-check mode** for web research (Hermes 0.20) as a skill + tool flag.
- **Sleep digest file** (OpenClaw DREAMS.md) — a human-readable "what consolidation did" log next to `janus memory log`.
- **Desktop/web UI**: subagent & process panels beside chat, conversation snooze, workboard (OpenClaw 10.1/10.5) — only after 2.3 provides the data.

**Skip:** Nous Portal/plan gauges, Hermes Cloud, Bot Screen, Microsoft Store lanes, OpenClaw "Decision Models" (undocumented), public conversation links, QuickJS Code Mode (Janus has RPC tool use), git re-sync with upstream.

---

## 5. Sequencing

1. **Week 1–2:** 2.1 memory flush, 2.4 usage report, 2.6 approval hardening — all small, independent, no schema migration except 2.6's approval record.
2. **Week 3–4:** 2.2 delivery ledger (schema migration in `SessionDB`), 2.5 skill versions.
3. **Week 5–7:** 2.3 background delegation (depends on 2.2 for cross-platform result delivery), then 3.2 profiles.
4. **Then** Tier 2 in order 3.1, 3.6, 3.3, 3.4, 3.5, 3.7.

Each item = one PR, default-off config key (except 2.2), entry in `docs/`, posture-audit awareness where security-relevant. Follow `AGENTS.md` for slash-command registration (`COMMAND_REGISTRY`) and the gateway double-message-guard.

## 6. Verification

- Unit/integration tests per item under the matching `tests/<area>/`, run with `scripts/run_tests.sh tests/<area>/`; full suite before merge. Invariant-style assertions only.
- `ruff check .` and `ty check` clean.
- Manual: `janus gateway start` with Telegram; kill the gateway mid-reply → reply arrives after restart (2.2); long session until `/compress` fires → MEMORY.md/journal gains entries (2.1); `delegate_task(background=true)` from CLI, then `janus delegations list` and parent gets the result (2.3); `janus usage --days 30` matches `cost_ledger.jsonl` sums (2.4); `janus skills undo` restores a miner-edited skill (2.5); approval issued in chat A does not unblock chat B (2.6).
- `janus security audit` reports the new approval defaults and flags any item left in an unsafe configuration.
