# PageMem — persistent per-site page memory for the browser agent

> Date: 2026-06-24 · Status: approved, implementing

## Goal
Persist what the agent learns about a site — stable element structure + successful
task playbooks — so repeat visits are faster and more reliable, instead of
re-deriving everything from ephemeral `@e`-ref snapshots each visit. Best-effort,
config-gated (default ON), never blocks browsing. No model in the capture path.

The accessibility-tree snapshot looks like `- button "Submit" [e2]`. The `[e2]` ref
is per-snapshot/ephemeral; the **stable** identity is `role` + accessible-name
(`button "Submit"`). PageMem keys on the stable identity and discards the ref.

## Components

### 1. `agent/page_memory.py` (new) — deterministic, no model
- `_domain(url)` → normalized domain key (lowercase host, strip `www.`).
- `_parse_elements(aria_snapshot)` → list of `{role, name}` for INTERACTIVE roles
  (button, link, textbox, searchbox, combobox, checkbox, radio, menuitem, tab,
  switch, option, …) with a non-empty name. Deterministic line parse of
  `- <role> "<name>" [eN]`.
- `capture(url, aria_snapshot)` → merge parsed elements into the per-domain profile
  (dedup by `role|name`, cap, bump last-seen). Auto-called on navigate.
- `record_playbook(url, task, steps)` → store a named semantic action sequence
  (`[{action, target, value?}]`, stable targets) + a success/fail counter. Secret
  scrub: drop `value` when the target role is `password`/`textbox "…password…"` or
  the value matches a secret pattern — never persist typed credentials.
- `recall(url)` → `{profile_elements, playbooks}` for the domain, best playbooks
  first (by net success). `format_recall(...)` → compact `📍 PageMem` text.
- `note_outcome(url, playbook_id, ok)` → bump counters; drop a playbook after N
  consecutive failures (staleness decay).
- Store: `get_janus_home()/page_memory/<domain>.json`. Caps: max elements/domain,
  max playbooks/domain. `max_domains` is enforced EAGERLY (oldest-by-mtime domain
  files deleted on each capture/record); playbooks self-prune via `note_outcome`
  decay. (No separate sleep step needed.)

### 2. Browser-tool hook (`tools/browser_tool.py` `browser_navigate`)
After a successful navigate, best-effort + gated: `capture(url, snapshot)` (write)
then `recall(url)` (read) → append `format_recall(...)` to the returned snapshot.
**Cache-safe** — it augments tool OUTPUT (appended), never the system prompt.

### 3. Tool `pagemem_remember(task, steps)` (toolset `browser`)
The agent records a winning playbook for the current site after it succeeds.
Explicit beats fuzzy success-detection. Registered in `toolsets.py` (`browser` +
~~`_JANUS_CORE_TOOLS` if appropriate~~ **`_JANUS_CORE_TOOLS`, and anywhere else
`browser_navigate` is exposed — not optional**).

> **Resolved 2026-07-31.** "If appropriate" was read as "leave it out," and it
> shipped in the `browser` toolset only — which no composite includes. So on all
> 22 toolsets that expose `browser_navigate` (including `janus-cli`, `janus-cron`,
> `janus-acp`) the capture half ran by default and rendered a recalled
> "playbooks" section that **nothing could ever populate**. The two seams are not
> independent: navigate captures and recalls, `pagemem_remember` is the only
> writer, so exposing one without the other ships a permanently half-dead
> feature. The invariant is now pinned by
> `tests/test_toolsets.py::TestPageMemSeamsShipTogether`. Cost of the fix: ~272
> tokens of tool schema, once per session, in the cached prefix.

### 4. Bounding (eager, not sleep)
`max_domains` is capped on every write (oldest domain files dropped); failing
playbooks decay via `note_outcome`. No separate sleep step.

## Staleness (load-bearing)
Structure is re-captured (merged) every navigate → always fresh. `role`+name
selectors survive DOM churn far better than CSS paths or `@e` refs. Playbooks carry
success counters and decay via `note_outcome` when they stop working — a lightweight
verifiable-reward analog. Recalled selectors are presented as "known approaches
(verify still present)", never blindly executed.

## Safety / privacy
Local-only JSON. Stores domains + interactive-element structure + task playbooks.
NEVER persists typed secret values (password-role fields and secret-pattern values
are scrubbed). Config-gated (`page_memory.enabled`, default true); prunable.

## Config
`page_memory: {enabled: true, max_domains: 200, max_elements: 60, max_playbooks: 12,
decay_fails: 3}`

## Testing
Deterministic `_parse_elements` from ariaSnapshot fixtures (roles, names, ignores
refs/non-interactive); `capture` merge + cap + dedup; `record_playbook` + secret
scrub; `recall` ordering by success; `note_outcome` decay/prune; `format_recall`;
the navigate hook injects-when-enabled and NEVER breaks navigate (best-effort);
sessions/domains isolated. No live browser — inject snapshot fixtures.

## YAGNI cuts (v1)
No cross-site graph, no dynamic web-tool generation, no auto playbook distillation
(agent records via the tool), no LLM in the capture path.
