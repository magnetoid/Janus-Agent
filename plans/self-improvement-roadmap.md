# Self-Improvement Loop — Roadmap

**Status:** COMPLETE (2026-07) — all three increments shipped. Increment 1 landed as
PR #5; Increments 2 & 3 landed as A-PR1..A-PR4 (`92785e3` recency-weighted recall,
`e162269` dialectic-gated synthesis, `8ec67cd` tool-failure penalty + compression
sink, `eb1e7aa` eval-trend governor freeze + live model_strengths sink). Superseded
by `docs/agi-roadmap-2026.md` (10 moves, all shipped) and the post-implementation
audit in `docs/self-improvement-evaluation-2026.md` — treat those as current; the
increment details below are historical.
**Owner:** learning loop (outcome tracking / sleep / curator / skill graph)
**Context:** Janus has a near-complete self-improvement engine that was mostly
default-off with several dangling feedback seams. This is the multi-increment
plan to make it a safe, graduated-trust flywheel. Autonomy model: **graduated
trust** — auto-admit only what passes verifiable rewards (self-test +
success-rate + red-team); everything else stays drafted for review.

---

## Increment 1 — Graduated-Trust Governor ✅ (PR #5, `claude/self-improvement-governor`)

The missing consumer of the collapse metrics + the enforcement point for
graduated promotion.

- `agent/self_improvement_governor.py` — OK / CAUTION / FROZEN from
  `outcome_tracker.learning_metrics()`; FROZEN on forgetting/collapse warnings;
  fail-open on exceptions.
- `agent/outcome_tracker.py` — warning thresholds lifted to shared constants.
- `agent/skill_graph.py` — `auto_promote_drafts()` (triple-gated, snapshot +
  archive-not-delete); `assess_promotability()` threshold kwargs.
- `agent/sleep.py` — gated PROMOTE step.
- `agent/skill_utils.py` — `.drafts` added to `EXCLUDED_SKILL_DIRS` (latent-bug fix).
- `janus_cli` — `learning.governor.*` config + `janus learning governor` inspect.

Default-off. Opt in: `learning.track_outcomes`, `learning.governor.enabled`,
`learning.governor.auto_promote`.

---

## Increment 2 — Deepen learning quality ✅ (shipped as A-PR1..A-PR3)

Goal: make what's learned more useful and durable. Each item is independent and
default-safe.

1. **Recency-weighted recall** — `agent/lessons.py::recall_lessons` (~line 181)
   ranks purely by lexical `overlap/sqrt(tokens)`; there is no temporal decay
   despite the "recency-weighted" description. Add a recency factor (e.g.
   multiply score by a half-life decay on `entry["ts"]`), keep the optional
   `hybrid_rerank`. Test: a newer lesson outranks an equally-lexically-matched
   older one.
2. **Cross-session lesson synthesis (the SYNTHESIZE stub)** — `agent/sleep.py`
   documents a SYNTHESIZE step (docstring line ~17) that isn't implemented. N
   near-duplicate failure lessons should collapse into one. Implement using the
   existing `deliberation.quorum_classify` / `red_team_claims`; write the
   synthesized lesson via `lessons.record_lesson(source="synthesis")`. Gate
   under `sleep.*`. Test: 3 same-`task_type` lessons → 1 synthesized.
3. **Compression learning sink** — `MemoryProvider.on_pre_compress` returns text
   that is injected into the summary then discarded (no durable sink).
   `agent/conversation_compression.py` (~line 431) calls it. Route a distilled
   insight into `lessons`/memory instead of dropping it. Keep cache-safety
   (post-compression only).
4. **Delegation learning** — `MemoryManager.on_delegation` (memory_manager.py
   ~line 646) only fires for **batch** delegations; single `delegate_task`
   (`tools/delegate_tool.py` ~line 631) never does. Fire it for single
   delegations too so parent-side learning is captured.

---

## Increment 3 — Reward integrity ✅ (shipped as A-PR3/A-PR4)

Goal: make the reward signal harder to game / richer.

1. **Observability → reward** — `plugins/observability/*` (langfuse, nemo) trace
   token cost / tool-failure / retry counts but feed nothing back. Surface
   tool-failure rate and retry count as auxiliary inputs to
   `outcome_tracker.classify_session` (or as a secondary penalty signal feeding
   `skill_success_trajectory`). Keep heuristic + cheap.
2. **Eval-trend consumption** — `eval_trend.run_trend()` records a longitudinal
   pass-rate curve (already cron-wired at `gateway/runner.py:17849`) but nothing
   *acts* on a downward trend. Let the governor read the eval trend as a second
   freeze signal (regression on the deterministic eval suite → CAUTION/FROZEN),
   complementing the outcome metrics. `eval_miner` autopin already drafts
   regression pins from failures.

---

## Cross-cutting invariants (apply to every increment)
- **Cache-safe:** all learning work is post-session or offline; never mutate the
  live conversation, system prompt, or toolset mid-session.
- **Default-off / opt-in:** gate every new behavior behind config; ship off.
- **Fail-open on errors, but the *safe* direction for a bad health signal is to
  pause self-modification** (the governor's asymmetry).
- **Archive-not-delete, snapshot before mutation** (curator backup pattern) for
  anything that changes the skills tree.
- **Invariant-style tests only** — no change-detector snapshots.

## Stub-detector flags found during research (all resolved — kept for history)
- ~~`agent/sleep.py` SYNTHESIZE step: documented, not implemented.~~ **Implemented** and
  red-team-gated at `agent/sleep.py:186-237` (verified 2026-07-31).
- ~~`lessons.recall_lessons`: lexical only, no recency (see Increment 2.1).~~ **Shipped** as
  `92785e3` (recency-weighted recall).
- `plugins/memory/mem0` and `plugins/memory/hindsight`: hook-less stubs. **Still the least
  integrated providers** (re-verified 2026-07-31) — the `MemoryProvider` ABC exposes six
  lifecycle hooks (`agent/memory_provider.py:156-279`); mem0 implements **0** of them and
  hindsight implements **1** (`on_session_switch`). They are not empty files (374 / 1777
  lines) so "stub" overstates it, but neither participates in the learning loop. The only
  item on this list that remains open.
