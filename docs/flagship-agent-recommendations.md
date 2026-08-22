# Janus Flagship Agent — Codebase Audit & Strategic Recommendations

> Status: Flagship strategy review · Owner: Product & Engineering · Last updated: 2026-06-20
> Scope: End-to-end audit of the Janus codebase covering architecture,
> capabilities, technical constraints, existing feature set, unmet requirements, and a
> prioritized roadmap for a flagship agent product.

---

> ## ⚠️ Correction Note — added 2026-06-23 (re-grounding required)
>
> A codebase fact-check found that **§1.1–1.2 (systems map + strengths) are accurate and
> trustworthy, but §1.3 (gaps), the branding, and parts of the roadmap are stale or wrong.**
> Re-derive the gaps from current `main` before acting on any recommendation. Specifically:
>
> - **Stale "gaps" that are actually shipped:** the *"cron hard-interrupt at 3 minutes"*
>   (#3) does **not exist** — cron uses a configurable ~600s *inactivity* timeout
>   (`JANUS_CRON_TIMEOUT`, can run for hours); *"/memory parked"* (#7) and *"auto-mining
>   hook pending"* (#9) are **shipped**; *"24 cloud-industry.com README URLs"* (#10/L2) is
>   now **zero**; *"no public WebSocket/SSE API"* (#12) — one **exists**. These trace to a
>   single stale `.torsor/active/context.md` note (2026-06-09).
> - **Branding is retired:** "Cloud Industry" → **Cloud Industry** (ADR 0004); C1's *"Nous
>   Portal"* → **Cloud Industry Portal**; and C1 "Janus Cloud" is partly **already started** at
>   cloud-industry.com (ADR 0005). Provider/model slugs (`nous`, Nous Hermes IDs) are
>   correctly preserved — keep those.
> - **LOC figures are off:** `run_agent.py` is ~5.3k LOC (not 12k); `cli.py` is ~16.2k
>   (not 11k); test count is ~28.6k (not 17k). The "12k/11k monoliths" framing is unreliable.
> - **Internal contradictions:** C4 + M1 (multi-tenant) reverse the single-tenant/OS-isolation
>   trust model the doc praises as a moat; §3 and §6 disagree on the core wedge (`…M5` vs `…C2`).
> - **Biggest blind spots:** no treatment of **token/cost economics** (despite the in-repo
>   consensus/smart-routing system) and no treatment of **interactive conversational
>   reliability** (the owner-reported "forgets agreements / rushes on long sessions" defect) —
>   the two things that most affect the *actual* current user. Quality is measured only as
>   autonomous task success.
>
> Treat the roadmap below as aspirational and enterprise-aimed; the near-term priorities are
> cost-aware routing and conversational reliability, not the SaaS/cloud sequence.
>
> **Second re-grounding pass — 2026-07-31.** Two more §1.3 gaps have since closed:
> **#4 ("no built-in observability stack")** — [plugins/observability/otlp/](../plugins/observability/otlp/)
> ships an OTLP exporter, and turn-level log correlation + optional JSON logs landed in
> `91d9ad1`; and the "supply-chain guards in CI" claim in §1.5 is now actually true
> ([.github/workflows/tests.yml](../.github/workflows/tests.yml) hard-gates lint + the full
> ~30.4k-test suite). Gaps **#1 (monoliths), #2 (sync loop), #5 (single-tenant),
> #6 (no collaboration primitives), #11 (three parallel chat surfaces), #13
> (deterministic-only evals)** were re-checked and **still hold**. For the self-improvement
> subsystem specifically, prefer
> [docs/self-improvement-evaluation-2026.md](self-improvement-evaluation-2026.md) §2.5 —
> its Status column is maintained; this document's §1.3 is not.

---

## Executive Summary

Janus is a sophisticated, model-agnostic, self-improving AI agent (~8,000 files, ~17k tests,
MIT-licensed) built by Cloud Industry. Its core differentiator
is a **closed learning loop** — autonomous skill mining, reflexion lessons, memory
distillation, dialectic admission gates, and the ACE playbook that tunes its own learning
prompts. The codebase is exceptionally well-engineered (strict dependency pinning, profile
isolation, plugin ABCs, hermetic test wrapper) and the architecture is mature across the
CLI / TUI / Desktop / ACP / Gateway / Cron surfaces.

The flagship opportunity is to convert Janus from "open-source power-user agent" into a
**mainstream, daily-driver agentic OS** by closing gaps in collaboration, observability,
monetization, and AI-native UX — without compromising its open-core / self-hosted values.

---

## 1. Codebase Audit

### 1.1 Core Systems Map

| Layer | Anchor files | Purpose |
|---|---|---|
| Agent loop | `run_agent.py` (~12k LOC, `AIAgent`), `model_tools.py`, `toolsets.py` | Synchronous tool-calling loop, 90 iterations, interrupt + budget + grace-call |
| Memory | `agent/memory_*.py`, `agent/curator.py`, `agent/playbook.py`, `agent/lessons.py`, `agent/memory_miner.py`, `agent/skill_miner.py`, `agent/sleep.py` | Honcho + 7 pluggable providers, ACE playbook, reflexion, mining |
| Skills | `skills/`, `optional-skills/`, `tools/skills_*.py`, `agent/skill_graph.py` | Bundled + mined + hub skills, curator lifecycle |
| Tools | `tools/`, `toolsets.py`, `tools/environments/{local,docker,ssh,modal,daytona,singularity}.py` | 40+ tools across ~28 toolsets, 6 terminal backends |
| CLI / TUI / Desktop | `cli.py` (~11k LOC), `ui-tui/` (Ink+React), `tui_gateway/` (JSON-RPC), `apps/desktop/` (Electron) | 3 user-facing surfaces |
| ACP | `acp_adapter/` | VS Code / Zed / JetBrains integration (9 modules) |
| Gateway | `gateway/run.py`, `gateway/platforms/`, `tui_gateway/ws.py` | 20+ messaging platforms |
| Cron / Kanban | `cron/`, `janus_cli/kanban*.py`, `tools/kanban_tools.py`, `plugins/kanban/` | Durable scheduling + multi-agent boards |
| Memory / Context Engines | `agent/context_engine.py`, `plugins/memory/`, `plugins/context_engine/` | Pluggable memory + context backends |
| Observability / Eval | `agent/evals.py`, `agent/eval_trend.py`, `agent/eval_miner.py`, `agent/outcome_tracker.py` | Regression harness, learning curves, forgetting metrics |
| Provider mesh | `agent/auxiliary_client.py` (145 symbols), `providers/`, `plugins/model-providers/`, `agent/{anthropic,bedrock,azure_identity,codex_runtime,google_code_assist}_adapter.py` | Multi-model + side-LLM routing |
| i18n / Skins | `agent/i18n.py`, `janus_cli/skin_engine.py`, `locales/*.yaml` (14 locales), `~/.janus/skins/*.yaml` | 14 locales, data-driven CLI theming |
| Security | `tools/{approval,path_security,url_safety,redact,threat_patterns,tirith_security,skills_guard}.py`, `SECURITY.md` | Approval gate, redaction, skills guard, plugin integrity |

### 1.2 Technical Strengths

- **Production-grade dependency hygiene** — exact-pinned `dependencies` (post Mini Shai-Hulud worm, 2026-05-12), upper-bounded `>=x,<next_major` everywhere else, CVE-mitigated Starlette pin, lazy-installed backends reduce blast radius.
- **Profile isolation** — `_apply_profile_override()` + `get_janus_home()` + `display_janus_home()` enforces multi-instance safety (5 bugs fixed in PR #3575).
- **Cache-aware system prompts** — strict invariants around mid-conversation context changes protect prompt caching.
- **Trust boundary clarity** — `SECURITY.md` §2.2 explicitly names OS-level isolation as the only boundary; in-process heuristics are correctly labeled as accident-prevention.
- **Pluggable everything** — ABCs for `MemoryProvider`, `BrowserProvider`, `ImageGen`, `VideoGen`, `TTS`, `ModelProvider`, `ContextEngine` — third parties can ship standalone plugin repos.
- **Per-file test isolation** — `scripts/run_tests_parallel.py` spawns a subprocess per test file with TZ=UTC, unset creds, temp `JANUS_HOME` — closes real local-vs-CI drift.
- **Self-improving loop is real** — outcome tracker → reflexion → mining → sleep cycle → ACE playbook, all gated by red-team admission and capped.

### 1.3 Gaps & Scalability Limitations

1. **`run_agent.py` / `cli.py` are ~12k / ~11k LOC monoliths** — `AIAgent.__init__` takes ~60 parameters; refactoring pressure is high. The "core conversation loop" co-mingles prompt assembly, tool execution, retry, caching, and credential routing.
2. **Synchronous agent loop** — `run_conversation()` is sync; subagents spawned via `delegate_task` are sync-blocking; cancellation only via parent interrupt. Limits scalability of long-running multi-agent work.
3. **Cron hard-interrupt at 3 minutes** — prevents runaway loops but caps the autonomous ambition of scheduled jobs; no checkpointed resume across the 3-min wall.
4. **No built-in observability stack** — `logs/{agent,errors,gateway}.log` exist but no metrics/tracing out of the box; users rely on `janus logs`. The `observability/` plugin directory exists but the canonical implementation lives in the example-plugins repo.
5. **Multi-user / RBAC story is single-tenant** — `SECURITY.md` §2.1 explicitly states "Janus Agent is a single-tenant personal agent." Profiles are full-instance isolation, not per-user within an instance.
6. **No first-class collaboration primitives** — there is no built-in shared session, shared whiteboard, comments/threads on messages, or review/approval flow between humans. Kanban is the closest primitive.
7. **`/memory` slash command is parked** (per `.torsor/active/context.md`); the memory surface is split across CLI subcommands (`memory`, `memory log`, `memory mine`) and slash variants with gaps.
8. **Gateway dual-message-guard complexity** — `gateway/run.py` + base adapter must both bypass approval/control commands; documented foot-gun for new commands.
9. **Auto-mining hook pending** — opt-in `memory.session_mining` is documented but the wiring is incomplete per active context.
10. **Cloud Industry brand removal incomplete** — `cloud-industry.com` URLs still in 24 README locations; provider/model IDs intentionally preserved (so breaking the removal would break users).
11. **ACP / Desktop / Dashboard are three parallel chat surfaces** — slash-command curation logic exists in three places (`acp_adapter`, `tui_gateway`, `apps/desktop/src/lib/desktop-slash-commands.ts`); risk of drift.
12. **No first-class WebSocket/SSE streaming API for remote apps** — there is `janus_cli/web_server.py` for dashboard, but no public agent API; programmatic integration doc (`website/docs/guides/python-library.md`) is thin.
13. **Eval suite is YAML + deterministic assertions** — strong for regression but not suited for human-judged quality or vision / multimodal evals.

### 1.4 User Interaction Flows Audit

| Surface | Strength | Gap |
|---|---|---|
| Classic CLI (`cli.py`) | Rich, skinnable, full slash surface | prompt_toolkit escape sequences limit visual fidelity |
| TUI (Ink) | Modern, embeddable in dashboard, full transcript | Requires Node toolchain for contributors |
| Desktop (Electron) | Cross-platform chat, `@assistant-ui/react` | Separate from CLI/TUI transcript, no cross-surface sync |
| Gateway (Telegram/Discord/Slack/...) | 20+ platforms, voice memo transcription | No shared "where I left off" across platforms for the same user |
| ACP (VS Code/Zed) | First-class IDE integration | No Visual Studio / Xcode native adapter |
| Web Dashboard | SPA + PTY-embedded TUI | Loopback-only by default; multi-user unsupported |

### 1.5 Security & Compliance Status

- **Strong:** CVE-mitigated Starlette pin, plugin integrity pinning (`security.plugin_integrity: warn/block`), supply-chain guards in CI, scoped credential env scrubbing, allowlist required per network-exposed adapter.
- **Adequate:** approval gate, output redaction, skills guard, `<untrusted-content>` fencing for web/email/webhook ingestion.
- **Gap:** no SOC 2 / ISO 27001 posture publicly; no SBOM published; no signed releases; no formal threat model document beyond the policy.
- **Gap:** no PII detection / DLP layer in the redact path beyond regex patterns.
- **Gap:** no audit-log export format — `janus logs` is for humans, not SIEM.

---

## 2. Strategic Recommendations — Prioritized

Priorities use **Impact × Feasibility / Cost**:

- **Critical** — ship-blocking competitive gap or revenue enabler
- **High** — meaningful differentiation, 1–2 quarter horizon
- **Medium** — table-stakes polish, 2–4 quarter horizon
- **Low** — nice-to-have, opportunistic

### CRITICAL

#### C1. Flagship Cloud Hosted Runtime ("Janus Cloud")

**Why critical:** The README is explicit that the only distribution is from source. There is no hosted option, no one-line installer, no try-before-install. Every competitor (OpenAI Operator, Anthropic Computer Use, Replit Agent, Devin, Manus, Genspark) ships a hosted URL. This is the single biggest acquisition bottleneck.

**Scope:** Hosted `janus` runtime with persistent profiles, web UI (embed the existing dashboard), OAuth sign-in, pay-per-use LLM routing through Cloud Industry Portal as the default backend.

**Implementation:** Wrap `tui_gateway` + `janus_cli/web_server.py` behind a stateless container service, persistent storage in Postgres / S3-compatible blob, signed JWT for cross-tab session resume. Reuse profile-isolation machinery.

**Effort:** 4–6 engineers × 2 quarters.

**ROI:** Direct revenue + 10× funnel expansion (try without install).

#### C2. Programmable Agent API & SDKs

**Why critical:** `python-library.md` is thin; there is no `npm install @janus/sdk`, no first-class OpenAI-compatible agent endpoint. Power users who want to embed Janus in their products have no clean path.

**Scope:** OpenAI-compatible `/v1/agent/runs` endpoint with streaming (SSE + WebSocket), Python + TypeScript SDKs, OAuth/API-key auth, per-call tool allowlist.

**Effort:** 2 engineers × 1 quarter (lift + adapt existing `web_server.py` + `tui_gateway/server.py`).

**ROI:** Unlocks third-party ecosystem, dev-tool integrations, IDE plugins beyond ACP.

#### C3. First-Class Observability ("Janus Insights")

**Why critical:** Self-improvement claims are unprovable without public metrics. Every LLM call, tool call, mining cycle, eval trend should be queryable.

**Scope:** OTLP-compatible traces + Prometheus metrics + structured JSON logs out of the box; `janus insights` dashboard built on existing `agent/insights.py`; public `/healthz` for hosted runtime; per-session cost & latency breakdown.

**Effort:** 1 engineer × 1 quarter.

**ROI:** Converts "trust me" learning-curve claims into "here's your dashboard" proof points; required for enterprise sales.

#### C4. Shared Workspace & Human Collaboration

**Why critical:** The agent operates on the user's filesystem alone. There is no way to share a session with a teammate, drop a comment on a message, or hand off a half-finished task. This blocks team plans.

**Scope:** Shared `session/` artifacts with role-based read/write, threaded comments on assistant messages, hand-off handoff tokens, "watch" mode for a sub-agent run.

**Effort:** 2 engineers × 1 quarter.

**ROI:** Unlocks team / SMB tier, multi-seat pricing.

---

### HIGH

#### H1. Async Agent Runtime & Long-Horizon Tasks

The synchronous loop + 3-min cron interrupt + 90-iteration budget is the right safety story but caps ambition. Add an `agent.long_run` mode that uses checkpointed state + worktree isolation, can run for hours, supports resume across gateway restarts, and broadcasts incremental progress.

**Effort:** 2 engineers × 1 quarter.

#### H2. Multimodal & Voice-First Surface

Voice mode exists but is CLI-only. Add browser/desktop push-to-talk, screen-share-aware computer-use (already started via `cua-driver`), and a vision-evals harness so the agent can be regression-tested on screenshots.

**Effort:** 2 engineers × 1 quarter.

#### H3. Native IDE Integrations (JetBrains, Visual Studio, Xcode)

ACP covers VS Code / Zed; the JetBrains family is larger by install base. Build a JetBrains plugin + a VS Code extension published to marketplace. Ship pre-built signed binaries.

**Effort:** 1 engineer × 1 quarter per platform.

#### H4. Curriculum Library: Curated Skill Bundles by Vertical

Today `optional-skills/` ships ad-hoc. Curate industry bundles (fintech, devops, data-eng, customer-support, legal-research) with vetted skills + model-strengths routing config + eval suite + onboarding skill.

**Effort:** 1 PM + 1 engineer × ongoing.

#### H5. Dedicated "Red Team" Mode

`red-teaming-godmode` skill exists but is a regular skill. Promote it to a first-class surface (`janus redteam`) with seed corpus, automated red-team pipelines, integration with the dialectic adversary for new skills/memories, and a public leaderboard for community-discovered issues.

**Effort:** 1 engineer × 1 quarter.

#### H6. Benchmark & Public Leaderboard

`agent/evals.py` is hermetic + local-only. Publish a public benchmark (`benchmarks.janus.dev`) running a standardized task suite (coding, web research, file ops, multi-step planning) against every supported model. Drives traffic + positions Janus as the model-agnostic benchmark runner.

**Effort:** 1 engineer × 1 quarter.

---

### MEDIUM

#### M1. SaaS-grade RBAC & Multi-Tenant Profiles

Profiles today are full-instance isolation. Add a "team" mode where one Janus instance serves N users with shared skills + per-user memory + per-user allowlist.

**Effort:** 2 engineers × 2 quarters.

#### M2. Marketplace for Skills & Plugins

The skills-hub (`tools/skills_hub.py`) is install-from-GitHub. Build a marketplace UI with ratings, versioned installs, paid premium skills (rev-share with authors), and content-hash integrity mirroring the existing plugin integrity pin.

**Effort:** 2 engineers × 2 quarters.

#### M3. SOUL.md 2.0 — Persona Studio

`/personality` already exists; turn it into a visual persona studio with multi-modal persona assets (avatar, voice, SOUL.md editor, voice preview, "personality packs" marketplace).

**Effort:** 1 engineer × 1 quarter.

#### M4. Native Mobile Clients (iOS / Android)

Gateway already reaches Telegram/Discord/etc. A native Janus app would own the surface (push, background audio, on-device STT).

**Effort:** 2 engineers × 3 quarters.

#### M5. Refactor `run_agent.py` into Composable Modules

Decompose the 60-parameter `AIAgent.__init__` and 12k-LOC loop into `Runtime` / `PromptBuilder` / `ToolDispatcher` / `CredentialRouter` / `BudgetGovernor` / `CacheController`. Unblocks everything else.

**Effort:** 1 engineer × 1 quarter.

#### M6. Public Roadmap + RFC Process

The codebase shows clear engineering maturity but no public RFC process. Adopt an `rfcs/` repo, monthly public roadmap, voting on proposals. Community-driven direction = contributors.

**Effort:** 0.5 engineer × ongoing.

#### M7. SOC 2 / SBOM / Signed Releases

Required for any enterprise deal. Pin every release to an SBOM, sign wheels with sigstore/cosign, formal SOC 2 Type I within 12 months.

**Effort:** 1 ops + 1 eng × 4 quarters (SOC 2 ramp).

#### M8. SDK Examples Gallery

Ten canonical, runnable examples: "Janus as a Slack bot," "Janus as a cron-driven data ETL," "Janus as a code-review webhook handler," etc. Each with deploy button to Janus Cloud.

**Effort:** 1 eng × 1 quarter.

---

### LOW (Opportunistic)

| ID | Recommendation | Effort |
|---|---|---|
| L1 | Polish auto-mining hook (parked) and ship `/memory` slash command | 1 eng × 2 weeks |
| L2 | Replace Cloud Industry URLs in README; preserve provider slugs | 0.5 eng × 1 week |
| L3 | Per-tool cost analytics (already in `usage_pricing.py`) surfaced in TUI as a real-time widget | 1 eng × 2 weeks |
| L4 | Public changelog + per-release eval-trend delta in `eval_trend.py` | 0.5 eng × 2 weeks |
| L5 | `janus doctor --fix` mode (auto-apply known fixes) | 1 eng × 1 month |
| L6 | Wire `Mermaid` / `Excalidraw` / `Manim` outputs as first-class artifacts | 1 eng × 1 month |
| L7 | Community-contributed eval leaderboard via PRs | 0.5 eng × ongoing |
| L8 | Cursor / Windsurf bridge (call out from ACP) | 1 eng × 1 month |

---

## 3. Implementation Roadmap (12-Month Horizon)

```
Q1   C1 (start)        C2 (start)      C3 (ship)        M5 (ship)
Q2   C1 (ship)         C2 (ship)       H1 (start)       H3 (start)
Q3   C4 (start)        H1 (ship)       H2 (ship)        H4 (launch v1)   M1 (start)
Q4   C4 (ship)         H5 (ship)       H6 (ship)        M2 (launch v1)   M7 (start)
```

Critical path: **C1 + C3 + M5 → C4 → M1**. The hosted runtime, observability story, and RBAC together unlock enterprise revenue and team plans — every other feature rides on top.

---

## 4. Success Metrics

| Track | Metric | 90-day target | 12-month target |
|---|---|---|---|
| Acquisition (C1, C2) | Hosted sign-ups / week | 500 | 25,000 MAU |
| Acquisition (C2) | SDK npm + PyPI downloads / week | 1,000 | 100,000 / month |
| Activation (C1) | Hosted first-conversation completion | 70% | 85% |
| Self-improvement claim proof (C3) | Public Insights dashboard uptime + eval-trend delta per release published | 1 release | Every release |
| Retention (C4, M1) | Weekly active users on team plan | 50 | 5,000 teams |
| Quality (H1, H5, H6) | Long-horizon task success rate | +15% | +40% |
| Quality (H6) | Public benchmark score vs. baseline | Baseline | +20% |
| Ecosystem (M2, H4) | Skills marketplace listings | 100 | 2,000 |
| Enterprise (M7) | SOC 2 Type I achieved | In progress | Achieved |

---

## 5. Risk Register

| Risk | Mitigation |
|---|---|
| Hosted runtime creates a two-product company (OSS + SaaS) and splits engineering focus | Open-core charter: cloud uses OSS as dependency, never forks |
| Self-hosted users feel second-class | Strict policy: every flagship feature ships OSS-first or dual-licensed |
| Supply-chain attack (Mini Shai-Hulud precedent) | Continue exact pinning + lazy-install + CI audit; publish SBOM |
| Brand consistency (ensure Janus naming is complete across surfaces) | Land L2 immediately; pursue domain acquisition for janus.dev |
| Multi-tenant RBAC introduces privilege bugs | Threat-model first, ship single-tenant extensions (M5, M6) before RBAC |
| OpenAI-compatible Agent API cannibalizes custom integrations | Publish it; it IS the integration |

---

## 6. Final Recommendation

The codebase is mature and the architectural foundations (profile isolation, plugin ABCs, cache-aware prompts, hermetic testing, dialectic admission gates) are competitive moats. The flagship wedge is **C1 (Janus Cloud) + C3 (Observability) + C2 (SDK/API)** — together they convert a powerful open-source tool into a defensible product with a clear path from hobbyist to enterprise. The closed self-improvement loop, properly instrumented and externally visible, becomes the durable differentiator no competitor can replicate quickly.