# Dialectic Learning Gate — Design Spec

**Status:** **SLICE 1 SHIPPED** (verified 2026-07-31) — ~~proposed~~. `agent/deliberation.py`
exists with `red_team_claims` / `quorum_classify` / `apply_verdicts`; the gate is wired into
`memory_miner.py:115`, `skill_miner.py:111`, `sleep.py:193`, `playbook.py:229`,
`twin_review.py:166`, and `lessons.screen_lesson` (the reflexion + compression sinks, which
this spec did not anticipate — added later to close gap G3). Config lives at
`janus_cli/config.py:1853` (`learning.dialectic.*`, default off) and the three aux tasks are
registered at `config.py:1398-1414`. **Slice 2 (§10) is still unstarted**: the `deliberate`
tool, frame-stability detection, and k-of-N cross-family quorum.
The design below is retained as the spec of record.
**Owner:** learning loop (auto-mine / outcome tracking / curator)
**Depends on:** `agent/auxiliary_client.py` (`call_llm`), existing miner injection points, `janus evals` (regression pinning)

---

## 1. Motivation

Janus's learning loop is currently **single-witness**. Every artifact that
enters the trusted set is admitted on the say-so of one LLM pass:

| Path | Today's gate | Failure mode |
|---|---|---|
| `agent/memory_miner.py::mine_session_memory` | none — facts written straight into the memory store | a wrong "fact" poisons every future session that recalls it |
| `agent/skill_miner.py::mine_session_skills` | `skill_verifier` (static well-formedness only) | overfit/duplicate/lucky-success skills pile up in drafts; curator later consolidates garbage with garbage |
| `agent/outcome_tracker.py::classify_session` | single SUCCESS/FAILURE call, `max_tokens=5` | mislabeled outcomes corrupt `skill_success_rate()`, which feeds curator decisions |

Bad input to the learning loop **compounds**: a mislabeled outcome skews a
skill's success trajectory, which changes what the curator archives, which
changes what future sessions load. The cheapest place to raise quality is
the **commit gate**, not the consumption side.

The mechanism: before committing a learned artifact, stage a structured
disagreement about it — an **Advocate** argues for admission, a **Skeptic**
argues against, an **Arbiter** rules on the stronger argument (never the
majority). Janus is the two-faced god; claims should survive being looked
at from both directions before they become "memory".

This is deliberately **not** `mixture_of_agents` (variance reduction —
averages correlated witnesses, washes out correct minority views) and not
`deep_reason` (depth from a single perspective). It is adversarial
admission control.

## 2. Scope

**In scope (this spec / slice 1):**

- New module `agent/deliberation.py` — stance prompts, the
  advocate→skeptic→arbiter exchange, verdict schema, quorum helper.
- Red-team gate wired into the three commit paths above.
- Config (`learning.dialectic.*`), aux-task registration for per-stance
  model pinning, eval specs, unit tests.

**Out of scope (slice 2, sketched in §10):**

- A user/agent-facing `deliberate` tool for arbitrary questions.
- Frame-stability ("glass half full") detection for end-user answers.
- Curator LLM-review-pass dialectics.

## 3. Concepts

- **Stance** — a system prompt that commits a reasoner to one direction
  (Advocate / Skeptic). Stances must *steelman*, not strawman: the Skeptic's
  prompt demands the strongest concrete objection, with evidence from the
  transcript, not generic doubt.
- **Exchange** — one bounded round: Advocate's case → Skeptic's rebuttal.
  No iterative debate in slice 1 (`rounds: 1`); empirically the first
  rebuttal carries most of the signal and every extra round is +2 calls.
- **Arbiter** — a third call that sees claim + case + rebuttal and emits a
  structured verdict. Instructed to reward the *stronger argument* and to
  prefer **revise** (narrow the claim) over accept/reject when the Skeptic
  found a real but fixable flaw.
- **Quorum** — for classification tasks (outcome labeling), agreement
  between two *opposed-prior* judges. Agreement → trust the label.
  Disagreement → the honest output is "contested", not a coin flip.
- **Model diversity** — stances should run on different model families
  when configured (see §7). Two calls to the same model share blind spots;
  Janus's provider breadth is the asset here.

## 4. Module: `agent/deliberation.py`

Follows the miner contracts: injectable `llm_caller`, never raises,
returns plain dicts.

```python
def red_team_claims(
    claims: list[dict],          # [{"id", "kind": "fact"|"skill"|..., "content", "context"}]
    *,
    transcript: str = "",        # evidence the stances may cite
    llm_caller=None,             # default: agent.auxiliary_client.call_llm
    rounds: int = 1,
) -> dict:
    """Batch advocate→skeptic→arbiter over a set of candidate artifacts.

    Returns {"verdicts": {id: Verdict}, "calls": N, "error": None|str}.
    On any infrastructure error: {"verdicts": {}, "error": str} — callers
    MUST fail open to today's behavior (see §8).
    """

def quorum_classify(
    question: str,
    transcript: str,
    *,
    stances: tuple[str, str] = ("charitable", "strict"),
    llm_caller=None,
) -> dict:
    """Two opposed-prior judges answer the same yes/no question.

    Returns {"agreed": bool, "label": True|False|None, "votes": [...]}.
    label is None when the judges disagree (contested).
    """
```

**Verdict schema** (one per claim):

```python
{
  "verdict": "accept" | "reject" | "revise",
  "confidence": "high" | "medium" | "low",
  "revised_content": str | None,   # only for "revise"
  "crux": str,                     # the load-bearing reason, one sentence
  "skeptic_objection": str,        # preserved for audit / draft frontmatter
}
```

**Batching is the cost-control invariant.** All candidate facts from one
session are red-teamed in ONE advocate call, ONE skeptic call, ONE arbiter
call (claims enumerated, verdicts returned as a JSON array). Per-claim
calls are forbidden — a 10-fact session must cost 3 calls, not 30. Skill
proposals (≤3 per session) ride in the same batch with `kind: "skill"`.

Total session-end overhead: **3 calls** for mining + **2 calls** for
outcome quorum (the existing single classify call is replaced, net +1).
All on auxiliary models.

### Stance prompts (sketch, final text in module)

- **Advocate** — "You argue these candidate memories/skills SHOULD be
  committed. For each: the strongest concrete evidence from the transcript
  that it is durable, general, and correct. Concede weak ones — a selective
  advocate is credible."
- **Skeptic** — "You argue these candidates should NOT be committed. For
  each, check: overfit to this one session? trivially lucky success?
  stale or contradicting existing memory (provided)? duplicate of an
  existing skill (names provided)? speculation phrased as fact? Cite the
  transcript. If you find no real flaw, say so — invented objections
  discredit you."
- **Arbiter** — "Rule per claim. Reward the stronger ARGUMENT, not the
  majority and not the longer text. Prefer 'revise' with a narrowed claim
  when the objection is real but fixable. Output JSON array of verdicts."
- **Outcome judges** — charitable ("did the assistant materially advance
  the user's goal?") vs strict ("would the user call this a success
  without qualification? Partial credit is FAILURE").

## 5. Integration points

### 5.1 `memory_miner.mine_session_memory` — the priority target

Facts are the only artifact written **directly into the trusted store**
today. After `_parse_facts` / before the `memory_store.add` loop:

```python
if _dialectic_enabled("memory"):
    rt = red_team_claims(claims_from(facts), transcript=transcript, llm_caller=llm_caller)
    if rt["error"] is None:
        facts = apply_verdicts(facts, rt["verdicts"])   # drop rejects, swap in revisions
        result["red_team"] = summarize(rt)              # {"accepted": N, "revised": M, "rejected": K}
```

Rejected facts are appended to the daily log
(`memories/daily/<date>.md`) under a `## rejected by red-team` heading
with the skeptic's objection — auditable, recoverable, but **not** in the
prompt-visible store. This mirrors the curator's "never delete, only
archive" invariant.

### 5.2 `skill_miner.mine_session_skills`

After `_parse_proposals`, before `write_skill_draft`:

- `reject` → draft still written (drafts are already a quarantine the user
  reviews) but to `.drafts/` with frontmatter
  `red_team: {verdict: reject, objection: ...}` and listed in
  `result["flagged"]` alongside the existing verifier flags. We do not
  silently discard — the user owns drafts.
- `revise` → the arbiter's narrowed description replaces the proposal's
  before rendering.
- The skeptic's prompt receives `existing_skill_names` so duplication is a
  first-class objection (today nothing checks semantic overlap at mine
  time; the curator catches it weeks later).

### 5.3 `outcome_tracker.classify_session`

Replace the single judge with `quorum_classify`. Mapping:

- agreed → True/False exactly as today (callers unchanged).
- disagreed → `None` (the existing "unclear" value — callers already
  handle it) **plus** the record written by `record_outcome` gains
  `"contested": true` so `skill_stats()` consumers can choose to exclude
  contested sessions from success-rate trajectories.

This is the purest quorum case and the cheapest win: the function's
contract (`True|False|None`) doesn't change.

### 5.4 Curator (deferred to slice 2)

The curator review fork is already an LLM pass over agent-created skills;
giving it a skeptic is the same pattern but touches the consolidation
prompt machinery — separate change, same `red_team_claims` primitive.

## 6. Configuration

`config.yaml`, new keys under the existing `learning` section
(`DEFAULT_CONFIG` in `janus_cli/config.py`; no `_config_version` bump —
additive keys deep-merge):

```yaml
learning:
  dialectic:
    enabled: false        # master switch, default OFF for rollout
    memory: true          # per-path gates (effective only when enabled)
    skills: true
    outcomes: true
    rounds: 1             # reserved; slice 1 supports exactly 1
```

## 7. Aux tasks & model diversity

Register three tasks in `_AUX_TASKS` (`janus_cli/main.py`) and resolve via
the existing `auxiliary.<task>` machinery:

```
("dialectic_advocate", "Dialectic advocate", "argues for learned-artifact admission"),
("dialectic_skeptic",  "Dialectic skeptic",  "argues against learned-artifact admission"),
("dialectic_arbiter",  "Dialectic arbiter",  "rules on learning red-team exchanges"),
```

Default resolution falls back to the normal auxiliary model (zero new
setup burden). Users who want real independence pin different families:

```yaml
auxiliary:
  dialectic_skeptic: {provider: openrouter, model: qwen3-coder}
  dialectic_arbiter: {provider: anthropic, model: claude-sonnet-4-6}
```

Docs should say plainly: same-model stances still help (role pressure
catches overfit/duplicates) but distinct families are what de-correlate
blind spots.

## 8. Invariants & failure policy

1. **Mining must never break a session** (existing contract) — all
   deliberation errors are caught; `red_team_claims` returns `error` and
   callers proceed exactly as today (fail-open), logging once.
2. **Fail-open is infrastructure-only.** A *successful* exchange whose
   arbiter rejects a claim is a real verdict — never overridden.
3. **Never silently destroy** — rejected facts go to the daily log;
   rejected skill drafts are written flagged, not skipped.
4. **Bounded cost** — 3 batched calls per session-end mine + 2 for outcome
   quorum, auxiliary models only, no tool access for stance agents.
5. **No prompt-cache impact** — everything runs post-session in the
   auto-mine path; the live conversation context is never touched.
6. **Arbiter rewards argument, not majority** — encoded in the prompt and
   pinned by evals (§9); this is the anti-mode-collapse guard.

## 9. Testing

**Unit (`tests/agent/test_deliberation.py`)** — scripted `llm_caller`
(the established miner-test pattern):

- verdict parsing: accept/reject/revise applied correctly; malformed
  arbiter JSON → infrastructure error → fail-open.
- batching: 10 claims → exactly 3 llm calls.
- `quorum_classify`: agree→label, disagree→None+contested.
- integration: `mine_session_memory` with a scripted rejecting arbiter
  writes nothing to the store and logs the rejection; with `error` set,
  behaves byte-identically to today.

**Evals (`janus evals`, shipped as examples)** — behavior pins that need a
live model:

```yaml
name: red-team-rejects-overfit-fact
prompt: <transcript where user says "use port 8080 for THIS demo only">
checks:
  - {type: regex, value: '"verdict":\s*"reject"'}
name: red-team-accepts-durable-fact
prompt: <transcript where user states a stable preference>
checks:
  - {type: regex, value: '"verdict":\s*"accept"'}
```

## 10. Slice 2 (sketch, for direction only)

- **`deliberate` tool** — expose the same exchange as a registered tool
  (own toolset, like `moa`): agent calls it on contested questions; panel
  runs via `delegate_task` leaf subagents for context isolation; returns
  the Verdict plus `frame_dependent: bool`.
- **Frame-stability mode** — run a claim under two opposed *framings*
  (not for/against): same verdict in both frames → frame-stable,
  high confidence; verdict flips → answer IS "it depends on the frame",
  surfaced explicitly instead of false confidence. The half-full/half-empty
  detector.
- **k-of-N quorum across model families** for high-stakes agent actions,
  gated by cost config.

## 11. Rollout

1. Land slice 1 behind `learning.dialectic.enabled: false`.
2. Dogfood with `enabled: true` + outcome quorum only; watch contested
   rate (expect 10–25%; >40% means the strict judge prompt is too harsh).
3. Enable memory + skill gates; compare `janus evals` suite + curator
   archive rate before/after.
4. Default-on for `outcomes` first (cheapest, contract-compatible), then
   `memory`, then `skills`.
