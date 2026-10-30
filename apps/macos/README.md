# Janus for Mac (native)

A native SwiftUI client for Janus on macOS 14+. It talks to the same backend
as the Electron app in `apps/desktop`, with the minimal look of a plain chat
app: a sidebar of chats, one centered thread, and a floating composer.

## Run

```bash
apps/macos/scripts/build-app.sh          # → apps/macos/build/Janus.app (release)
open apps/macos/build/Janus.app
```

`CONFIG=debug` gives a faster build. `JANUS_SOURCE_ROOT=/path/to/janus` picks
the checkout the app runs by default (it must have a `.venv`); the default is
this repository. Settings (⌘,) can change it later. The build needs only the
Command Line Tools (`swift`), not Xcode.

## How it works

On launch the app starts `janus dashboard --no-open --host 127.0.0.1 --port N`,
just like `apps/desktop/electron/main.cjs` does:

- It picks a random loopback port and a random per-launch token, passed as
  `JANUS_DASHBOARD_SESSION_TOKEN`.
- It polls `GET /api/status` until the backend is ready.
- It sets `JANUS_DESKTOP=1`, so the backend also ticks cron. The tick takes
  `cron/.tick.lock`, so jobs never fire twice next to a running gateway.

The backend is stopped when the app quits, including on `kill` or Ctrl-C.

The app reaches the backend two ways:

- **REST** (`X-Janus-Session-Token` header): the session list (cron runs
  excluded, as in the desktop app), stored messages, rename, delete, the
  model catalog, skills, and cron jobs.
- **WebSocket** `/api/ws?token=…`: JSON-RPC 2.0 to `tui_gateway/server.py`.
  It covers `session.create`/`resume`, `prompt.submit`, `session.interrupt`,
  `approval.respond`, `clarify.respond`, `model.options`, and `slash.exec`
  for `/model`. It also carries the event stream (`message.delta`,
  `reasoning.delta`, `tool.start`/`complete`, `subagent.*`,
  `approval.request`, `clarify.request`, `message.complete`, `session.info`).

## What's there

- Chats grouped by Today / Yesterday / Previous 7 days / Earlier, with search.
  Right-click to rename, copy the session ID, or delete.
- Streaming replies: Markdown (headings, lists, quotes, code blocks with
  copy), a collapsible thought process, and inline tool activity you can
  expand to see each tool's output.
- Approval cards for risky commands (allow once, for this chat, always, or
  deny) and clarify questions.
- Stop (⌘.), model picker, context-window meter, and cost when known.
- Skills (toggle on and off) and Scheduled (cron jobs).
- Settings: Janus source folder or `janus` command, profile, working folder,
  and restart the backend (⇧⌘R).

## Development aids

| Variable | Effect |
| --- | --- |
| `JANUS_MAC_SNAPSHOT=/tmp/x.png` | Writes a PNG of the window after `JANUS_MAC_SNAPSHOT_DELAY` seconds (default 8). `JANUS_MAC_SNAPSHOT_REPEAT=N` repeats it every N seconds. No screen-recording permission is needed. |
| `JANUS_MAC_OPEN=first\|<session id>` | Opens that chat on launch. |
| `JANUS_MAC_PROBE="text"` | Sends one message on launch. |

Run the binary directly to use them:
`JANUS_MAC_OPEN=first build/Janus.app/Contents/MacOS/Janus`.

## Not yet

These Electron features aren't in the native app yet: onboarding and provider
setup (use `janus model` in a terminal), file previews and the right rail,
voice, multiple profiles at once, attachments, and the agents, artifacts,
messaging, and command-center views.
