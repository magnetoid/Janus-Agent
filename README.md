<div align="center">
<pre>
     ██╗ █████╗ ███╗   ██╗██╗   ██╗███████╗
     ██║██╔══██╗████╗  ██║██║   ██║██╔════╝
     ██║███████║██╔██╗ ██║██║   ██║███████╗
██   ██║██╔══██║██║╚██╗██║██║   ██║╚════██║
╚█████╔╝██║  ██║██║ ╚████║╚██████╔╝███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚══════╝
</pre>
<strong>The AI agent that gets better every time you use it · by Cloud Industry</strong>
</div>

# Janus Agent

<p align="center">
  <a href="https://github.com/magnetoid/janus"><img src="https://img.shields.io/badge/Docs-GitHub-FFD700?style=for-the-badge" alt="Documentation"></a>
  <a href="https://discord.gg/imbalabs"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/magnetoid/janus/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://github.com/magnetoid/janus"><img src="https://img.shields.io/badge/Built%20by-Cloud%20Industry-blueviolet?style=for-the-badge" alt="Built by Cloud Industry"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>
</p>

> ### Every other AI agent is an amnesiac.
> It forgets your project the moment the session ends, repeats yesterday's mistakes, and never gets any smarter no matter how much you use it.
>
> **Janus is different. Janus compounds.**
>
> It remembers who you are across every conversation. It turns each failure into a lesson it recalls before the next similar task. It writes reusable skills from work that went well. It measures whether it's actually improving — with real benchmarks, not vibes. And, uniquely, it can **propose careful improvements to its own skills and prompts, prove they help on an eval suite, and — with your approval — rewrite itself to be better tomorrow than it was today.**
>
> Point it at a $5 VPS. Talk to it from Telegram while it works on a cloud VM. Watch it get sharper, week over week.

---

## 🧠 The one thing no other agent does: it learns

Janus ships a **closed self-improvement loop** — the machinery that turns raw experience into durable, measurable capability. Flip it on with a single command:

```bash
janus learning enable
```

…and from that moment your agent starts to accumulate:

- **📓 Lessons from its own mistakes.** A failed task is distilled into one transferable *"do X instead"* lesson — and **automatically surfaced the next time a similar task comes up**, so the same mistake doesn't happen twice.
- **🛠️ Skills from its own successes.** Multi-step work that worked becomes a reusable, self-testing skill draft for your review — the agent literally writes new abilities for itself.
- **😴 Sleep-time consolidation.** While idle, it re-reads recent sessions, merges what it learned, retires contradictions, and prunes noise — the same "consolidate offline" trick that cuts inference cost and lifts accuracy.
- **🎯 A memory of *you*.** Facts, preferences, and a deepening user model that carries across Telegram, the terminal, and every other surface — searchable by meaning, not just keywords.
- **📈 Proof it's working.** A longitudinal eval **learning curve**, a saturation-proof **time-horizon score** (the longest task it can reliably do), and a **fail-closed regression gate** that freezes self-modification the moment quality slips. Improvement you can graph — not a marketing claim.

<div align="center">

**experience → lesson → recall → better behavior → measured → governed → repeat**

</div>

### And the part that sounds like science fiction (but ships today, safely)

**Governed self-code-improvement.** Janus can propose variants of its *own* skills, prompts, and policies, run them against its eval suite in an isolated sandbox, and promote only the ones that measurably help. Every promotion is quadruple-gated — **it must not regress the evals, the health governor must be green, the spend/safety floor must be clear, and a human must approve it** — it can *never* touch core code, and every change is fully reversible. This is the Darwin-Gödel-Machine idea from the research frontier, wrapped in a cage strong enough to actually run.

```bash
janus self-improve list      # review what the agent wants to change about itself
janus self-improve show <id> # see the diff + the eval evidence
janus self-improve promote   # apply it — only if every gate is green
```

> Off by default. Gated behind `learning.self_improve.enabled` and human approval. Because an agent that edits itself should have to earn that trust.

---

## ⚡ What it can do for you

<table>
<tr>
<td width="50%"><h3>💬 Lives where you already are</h3>
Telegram, Discord, Slack, WhatsApp, Signal, email, and a gorgeous terminal UI — <b>all from one gateway process</b>. Send a voice memo from your phone; it transcribes and acts. Start a thread on your laptop, continue it from Telegram on the train. Your agent isn't trapped in a browser tab.</td>
<td width="50%"><h3>🌍 Runs anywhere, costs almost nothing</h3>
A $5 VPS, a GPU cluster, or <b>serverless infrastructure that hibernates when idle</b> and wakes on demand. Six terminal backends — local, Docker, SSH, Singularity, Modal, Daytona. Your agent's whole environment can sleep between sessions and cost you cents.</td>
</tr>
<tr>
<td width="50%"><h3>🔀 Any model, zero lock-in</h3>
Cloud Industry Portal, OpenRouter (200+ models), NVIDIA NIM, z.ai/GLM, Kimi, MiniMax, Hugging Face, OpenAI, or your own endpoint. Switch with <code>janus model</code> — no code changes. And it <b>learns which model is best for which kind of task</b>, then routes cheap work to cheap models and hard work to strong ones.</td>
<td width="50%"><h3>🧭 Delegates and parallelizes</h3>
Spawns isolated subagents for parallel workstreams and folds their results back in. Writes Python that calls tools over RPC, collapsing multi-step pipelines into a single zero-context-cost turn. Big jobs fan out; you get one clean answer.</td>
</tr>
<tr>
<td width="50%"><h3>⏰ Works while you sleep</h3>
A built-in cron scheduler runs tasks unattended and delivers results to any platform — daily reports, nightly backups, weekly research digests, all described in plain English. It even holds a long-term <b>north-star goal</b> and checks in on your progress weekly.</td>
<td width="50%"><h3>🛡️ Safe enough to leave running</h3>
Rolling <b>USD spend caps</b> and a <b>master kill switch</b> gate every unattended action, so an autonomous run can't run away with your money. Command approval, container isolation, and DM pairing keep the blast radius small. Turn it loose without holding your breath.</td>
</tr>
</table>

---

## 🚀 Quick Install

Janus installs from source. (Hosted one-line installers will follow once Cloud Industry has install hosting.)

### Linux, macOS, WSL2, Termux

```bash
git clone https://github.com/magnetoid/janus.git
cd janus
./setup-janus.sh
```

`setup-janus.sh` installs uv, creates the venv, installs the `.[all]` extra, and symlinks `janus` into `~/.local/bin`. (Paste the commands one per line — don't append a `#` comment after `./setup-janus.sh`, as interactive zsh treats the unquoted `.[all]` as a glob and aborts with `no matches found`.)

On Termux, install the curated `.[termux]` extra instead of `.[all]` (the full extra pulls Android-incompatible voice dependencies):

```bash
git clone https://github.com/magnetoid/janus.git && cd janus
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -e ".[termux]"
```

### Windows (native, PowerShell)

```powershell
git clone https://github.com/magnetoid/janus.git
cd janus
uv venv .venv --python 3.11
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[all]"
```

---

## 🎬 Getting Started

Two minutes from clone to a conversation that remembers you:

```bash
janus                # Interactive CLI — start a conversation
janus model          # Choose your LLM provider and model
janus learning enable # ✨ Turn on the self-improving loop
janus setup          # Full setup wizard — configures everything at once
janus gateway        # Start the messaging gateway (Telegram, Discord, …)
janus tools          # Configure which tools are enabled
janus doctor         # Diagnose any issues
janus update         # Update to the latest version
```

📖 **[Full documentation →](website/docs/getting-started/quickstart.md)**

---

## 🔬 The self-improving loop, in detail

Everything above is real, on by default once you run `janus learning enable`, and — crucially — **measurable**. Each row is a command you can run today.

| Capability | What it does | Try it |
| --- | --- | --- |
| **Lesson recall** | Failed tasks become transferable lessons that are **auto-injected** into the next similar task — mistakes stop repeating. | auto on failure · `recall_lessons` tool |
| **Lesson efficacy** | Each recalled lesson is scored by whether the sessions it rode with succeeded; helpful ones rise, unhelpful ones sink. | automatic |
| **Skill mining** | Distills reusable **skills** (`SKILL.md` + script) from multi-step work that succeeded — for your review. | `janus skills mine` |
| **Sleep consolidation** | Offline, idle-time processing graduates facts + skills, retires contradictions, and prunes noise. | `janus sleep --now` |
| **Measurable improvement** | A pass-rate **learning curve**, per-eval **pass^k reliability** with a noise floor, and forward/backward-transfer + forgetting metrics. | `janus evals trend` · `janus learning stats` |
| **Time-horizon KPI** | The longest task (in human-minutes) the agent reliably passes — a saturation-proof score that keeps rising as it grows. | `janus evals horizon` |
| **Regression gate** | A **fail-closed** CI/cron gate that fails the moment a behavior regresses — and freezes self-modification with it. | `janus evals gate` |
| **Learned routing** | Learns which model wins per task category from **real outcomes and cost**, then routes accordingly (cheap for easy, strong for hard, an ensemble for the hardest). | `janus consensus route "…"` |
| **Hybrid memory recall** | Fuses keyword (BM25) and **semantic** search so a paraphrase with zero shared words still finds the right memory. | automatic (optional embeddings) |
| **Dialectic deliberation** | An advocate and a skeptic argue, an arbiter rules — and it tells you when a question is genuinely frame-dependent instead of faking confidence. | `deliberate` tool (`moa` toolset) |
| **Red-team learning gate** | Mined facts/skills must survive an advocate/skeptic/arbiter exchange before they're learned — bad input is filtered at the door. | `learning.dialectic.enabled: true` |
| **Governed self-improvement** | Proposes, eval-tests, and — with your approval — promotes improvements to its **own** skills/prompts/policies. Never core code. Fully reversible. | `janus self-improve list` |
| **Safety floor** | Rolling daily/monthly **spend caps** + a master **freeze** switch gate every autonomous action. | `janus autonomy status` · `janus autonomy freeze` |
| **Aspirations & interests** | Holds a long-term goal and checks in weekly; tracks a field and researches it on a schedule, asking before it learns. | `janus aspire set "<goal>"` · `janus interest add "<field>"` |

The slash variants (`/memory`, `/aspire`, `/interest`, …) work from the messaging gateway and the TUI too.

---

## 🔑 Skip the API-key scavenger hunt — Cloud Industry Portal

Janus works with whatever provider you want — that's not changing. But if you'd rather not collect five separate API keys for the model, web search, image generation, TTS, and a cloud browser, **[Cloud Industry Portal](https://portal.cloud-industry.com)** covers all of them under one subscription:

- **300+ models** — pick any with `/model <name>`
- **Tool Gateway** — web search (Firecrawl), image generation (FAL), text-to-speech (OpenAI), cloud browser (Browser Use) — all routed through your sub. No extra accounts.

One command from a fresh install:

```bash
janus setup --portal
```

That logs you in via OAuth, sets the provider, and turns on the Tool Gateway. Check what's wired up any time with `janus portal info`. You can still bring your own keys per-tool whenever you want — the gateway is per-backend, not all-or-nothing.

---

## 📟 CLI vs Messaging — quick reference

Two entry points: start the terminal UI with `janus`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, most slash commands are shared.

### Blob — two ways to join a workspace

[Blob](https://github.com/magnetoid/blob) is a self-hosted, agent-native team chat. Janus can be a member of a Blob workspace in one of two ways, and they are alternatives, not layers:

- **Dial in from anywhere (recommended).** Janus runs where you run it — a laptop, a VPS, a container — and holds a socket to Blob's `/ws/agent`. No public URL, no inbound port. Enable it with two environment variables, which is exactly what Blob's admin page prints when you add a Janus agent:

  ```bash
  export BLOB_URL=https://chat.example.com
  export BLOB_BOT_TOKEN=…            # from Blob → Admin → Apps & agents → Install agent → Janus
  janus gateway start                # `janus gateway status` shows "Blob: connected"
  ```

  Optional: `BLOB_AGENT_NAME`, `BLOB_AGENT_DESCRIPTION`, `BLOB_AGENT_VERSION` — what Blob shows for this agent. Or the same under `platforms: blob:` in `~/.janus/config.yaml`.

- **Let Blob host it.** `blob-app.json` in this repo is a Blob app manifest: Blob builds Janus from `docker-compose.coolify.yml` and talks AG-UI to it at `/v1/agui`. Use this when the Blob server should own the process. Its configuration is Blob's, not the environment above.

Either way Janus is a real member of the workspace — mentions, DMs, threads and the run card all work — and answers with the asker's permissions, never more.

- **Professional minimal CLI** with a data-driven skin engine — `/skin classic` restores the pre-redesign look, `/skin list` shows all themes.

| Action | CLI | Messaging platforms |
| --- | --- | --- |
| Start chatting | `janus` | `janus gateway setup` + `janus gateway start`, then message the bot |
| Start fresh conversation | `/new` or `/reset` | `/new` or `/reset` |
| Change model | `/model [provider:model]` | `/model [provider:model]` |
| Set a personality | `/personality [name]` | `/personality [name]` |
| Retry or undo the last turn | `/retry`, `/undo` | `/retry`, `/undo` |
| Compress context / check usage | `/compress`, `/usage`, `/insights` | `/compress`, `/usage`, `/insights` |
| Browse skills | `/skills` or `/<skill-name>` | `/<skill-name>` |
| Interrupt current work | `Ctrl+C` or send a new message | `/stop` or send a new message |

For the full command lists, see the [CLI guide](website/docs/reference/cli-commands.md) and the [Messaging Gateway guide](website/docs/user-guide/messaging/index.md).

---

## 📚 Documentation

| Section | What's Covered |
| --- | --- |
| [Quickstart](website/docs/getting-started/quickstart.md) | Install → setup → first conversation in 2 minutes |
| [CLI Usage](website/docs/reference/cli-commands.md) | Commands, keybindings, personalities, sessions |
| [Configuration](website/docs/user-guide/configuration.md) | Config file, providers, models, all options |
| [Messaging Gateway](website/docs/user-guide/messaging/index.md) | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |
| [Security](website/docs/user-guide/security.md) | Command approval, DM pairing, container isolation |
| [Tools & Toolsets](website/docs/user-guide/features/tools.md) | 40+ tools, toolset system, terminal backends |
| [Skills System](website/docs/user-guide/features/skills.md) | Procedural memory, Skills Hub, creating skills |
| [Memory](website/docs/user-guide/features/memory.md) | Persistent memory, user profiles, best practices |
| [MCP Integration](website/docs/user-guide/features/mcp.md) | Connect any MCP server for extended capabilities |
| [Cron Scheduling](website/docs/user-guide/features/cron.md) | Scheduled tasks with platform delivery |
| [Architecture](website/docs/developer-guide/architecture.md) | Project structure, agent loop, key classes |
| [The AGI roadmap](docs/agi-roadmap-2026.md) | The full self-improvement design + research grounding |

---

## 📦 Migrating an existing install

Coming from an older `~/.hermes` install or from OpenClaw? Janus imports your config, memory, skills, sessions, and keys — non-destructively.

```bash
janus migrate hermes         # Import ~/.hermes into ~/.janus (add --dry-run to preview)
janus claw migrate           # Import an OpenClaw install (settings, memories, skills, keys)
```

`janus setup` also auto-detects both during first-time setup and offers to migrate before configuration begins. Top-level directories are merged (new files copied in, existing kept); Nous Hermes model IDs and the `nous` provider slug are preserved so your model config keeps working. See `janus migrate hermes --help` / `janus claw migrate --help` for all options.

---

## 🤝 Contributing

We welcome contributions! See the [Contributing Guide](website/docs/developer-guide/contributing.md) for setup, code style, and PR process.

```bash
git clone https://github.com/magnetoid/janus.git
cd janus
./setup-janus.sh          # installs uv, venv, .[all], symlinks ~/.local/bin/janus
./janus                   # auto-detects the venv — no need to `source` first
scripts/run_tests.sh      # run the test suite the way CI does
```

Manual path: `uv venv .venv --python 3.11 && source .venv/bin/activate && uv pip install -e ".[all,dev]"`.

---

## 🌐 Community

- 💬 [Discord](https://discord.gg/imbalabs)
- 📚 [Skills Hub](https://agentskills.io)
- 🐛 [Issues](https://github.com/magnetoid/janus/issues)

---

## 📄 License

MIT — see [LICENSE](LICENSE). Built with obsessive care by **Cloud Industry**.

<div align="center">
<sub>Most agents forget. Janus remembers, learns, and rewrites itself to do better. <b>Give it a week and see the difference.</b></sub>
</div>
