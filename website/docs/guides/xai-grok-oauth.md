---
sidebar_position: 16
title: "xAI Grok OAuth (SuperGrok / X Premium+)"
description: "Sign in with your SuperGrok or X Premium+ subscription to use Grok models in Janus Agent — no API key required"
---

# xAI Grok OAuth (SuperGrok / X Premium+)

Janus Agent supports xAI Grok through a browser-based OAuth login flow against [accounts.x.ai](https://accounts.x.ai), using either a **SuperGrok subscription** ([grok.com](https://x.ai/grok)) or an **X Premium+ subscription** (linked X account). No `XAI_API_KEY` is required — log in once and Janus automatically refreshes your session in the background.

When you sign in with an X account that has Premium+, xAI automatically links the subscription status to your xAI session, so the OAuth flow works the same as it does for direct SuperGrok subscribers.

The transport reuses the `codex_responses` adapter (xAI exposes a Responses-style endpoint), so reasoning, tool-calling, streaming, and prompt caching work without any adapter changes.

The same OAuth bearer token is also reused by every direct-to-xAI surface in Janus — TTS, image generation, video generation, and transcription — so a single login covers all four.

## Overview

| Item | Value |
|------|-------|
| Provider ID | `xai-oauth` |
| Display name | xAI Grok OAuth (SuperGrok / X Premium+) |
| Auth type | OAuth 2.0 device code ([RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628)); loopback PKCE with `--loopback` |
| Transport | xAI Responses API (`codex_responses`) |
| Default model | `grok-4.3` |
| Endpoint | `https://api.x.ai/v1` |
| Auth server | `https://accounts.x.ai` |
| Requires env var | No (`XAI_API_KEY` is **not** used for this provider) |
| Subscription | [SuperGrok](https://x.ai/grok) or [X Premium+](https://x.com/i/premium_sign_up) — see note below |

## Prerequisites

- Python 3.9+
- Janus Agent installed
- An active **SuperGrok** subscription on your xAI account, **or** an **X Premium+** subscription on the X account you sign in with (xAI links the subscription automatically)
- A browser on any device (the sign-in page can be opened on your phone or laptop; Janus only needs to reach `auth.x.ai`)

:::warning xAI may restrict OAuth API access by tier
xAI's backend enforces its own allowlist on the OAuth API surface and has been seen to reject standard SuperGrok subscribers with `HTTP 403` (see issue [#26847](https://github.com/magnetoid/janus/issues/26847)) even though the in-app subscription is active. If OAuth login succeeds in the browser but inference returns 403, set `XAI_API_KEY` and switch to the API-key path (`provider: xai`) — that surface is not subject to the same gating today.
:::

## Quick Start

```bash
# Launch the provider and model picker
janus model
# → Select "xAI Grok OAuth (SuperGrok / X Premium+)" from the provider list
# → Janus prints a short code and opens accounts.x.ai/oauth2/device
# → Check that the code matches and approve
# → Pick a model (grok-4.3 is at the top)
# → Start chatting

janus
```

After the first login, credentials are stored under `~/.janus/auth.json` and refreshed automatically before they expire.

## Logging In Manually

You can trigger a login without going through the model picker:

```bash
janus auth add xai-oauth
```

Janus prints a code and a link, for example:

```text
To sign in to xAI Grok, open this page and confirm the code:
  https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH
  Code: ABCD-EFGH
Waiting for approval (code expires in 30 min)...
```

Open the link (Janus opens it for you on a desktop), sign in, check that the code matches, and approve. Janus polls xAI in the background and finishes on its own; there is nothing to paste back.

The dashboard and the desktop app use the same flow: they show the code and open the page, then pick up the approval automatically.

### Remote / headless sessions

The device-code flow needs no local callback, so it works the same over SSH, in containers, and in browser-only consoles (Cloud Shell, Codespaces, EC2 Instance Connect). Janus prints the link instead of opening a browser; open it on any device. No port forwarding is needed.

### Browser-redirect login (`--loopback`)

The older loopback PKCE flow is still available:

```bash
janus auth add xai-oauth --loopback
janus model --loopback
```

It listens on `127.0.0.1:56121` and waits for xAI to redirect back. xAI's consent page often shows the authorization code on the page instead of redirecting. In that case, paste **just the bare code** at the `Callback URL:` prompt that appears after the wait times out, or start with `--manual-paste` to skip the listener. Janus accepts the full callback URL, a bare `?code=...&state=...` query fragment, or a bare code.

For running the loopback flow on a remote host (SSH port forwarding, ProxyJump chains), see [OAuth over SSH / Remote Hosts](./oauth-over-ssh.md).

## How the Login Works

1. Janus asks `auth.x.ai` for a device code and shows you the code and the verification link.
2. You sign in at `accounts.x.ai` (or confirm your existing session), check the code, and approve.
3. Janus polls xAI's token endpoint until you approve, deny, or the code expires (30 minutes). The tokens are saved to `~/.janus/auth.json`.
4. From then on, Janus refreshes the access token in the background — you stay signed in until you `janus auth remove xai-oauth` or revoke access from your xAI account settings.

## Checking Login Status

```bash
janus doctor
```

The `◆ Auth Providers` section will show the current state of every provider, including `xai-oauth`.

## Switching Models

```bash
janus model
# → Select "xAI Grok OAuth (SuperGrok / X Premium+)"
# → Pick from the model list (grok-4.3 is pinned to the top)
```

Or set the model directly:

```bash
janus config set model.default grok-4.3
janus config set model.provider xai-oauth
```

## Configuration Reference

After login, `~/.janus/config.yaml` will contain:

```yaml
model:
  default: grok-4.3
  provider: xai-oauth
  base_url: https://api.x.ai/v1
```

### Provider aliases

All of the following resolve to `xai-oauth`:

```bash
janus --provider xai-oauth        # canonical
janus --provider grok-oauth       # alias
janus --provider x-ai-oauth       # alias
janus --provider xai-grok-oauth   # alias
```

## Direct-to-xAI Tools (TTS / Image / Video / Transcription / X Search)

Once you're logged in via OAuth, every direct-to-xAI tool reuses the same bearer token automatically — there is **no separate setup** unless you'd rather use an API key.

To pick a backend for each tool:

```bash
janus tools
# → Text-to-Speech       → "xAI TTS"
# → Image Generation     → "xAI Grok Imagine (image)"
# → Video Generation     → "xAI Grok Imagine"
# → X (Twitter) Search   → "xAI Grok OAuth (SuperGrok / X Premium+)"
```

If OAuth tokens are already stored, the picker confirms it and skips the credential prompt. If neither OAuth nor `XAI_API_KEY` is set, the picker offers a 3-choice menu: OAuth login, paste API key, or skip.

:::note Video generation is off by default
The `video_gen` toolset is disabled by default. Enable it in `janus tools` → `🎬 Video Generation` (press space) before the agent can call `video_generate`. Otherwise the agent may fall back to the bundled ComfyUI skill, which is also tagged for video generation.
:::

:::note X search auto-enables when xAI credentials are present
The `x_search` toolset auto-enables whenever xAI credentials (a SuperGrok / X Premium+ OAuth token or `XAI_API_KEY`) are configured. Disable explicitly via `janus tools` → `🐦 X (Twitter) Search` (press space) if you don't want this. The tool routes through xAI's built-in `x_search` Responses API — it works with **either** your SuperGrok / X Premium+ OAuth login or a paid `XAI_API_KEY`, and prefers OAuth when both are configured (uses your subscription quota instead of API spend). The tool schema is hidden from the model when no xAI credentials are configured, regardless of whether the toolset is enabled.
:::

### Models

| Tool | Model | Notes |
|------|-------|-------|
| Chat | `grok-4.3` | Default; auto-selected when you log in via OAuth |
| Chat | `grok-4.20-0309-reasoning` | Reasoning variant |
| Chat | `grok-4.20-0309-non-reasoning` | Non-reasoning variant |
| Chat | `grok-4.20-multi-agent-0309` | Multi-agent variant |
| Image | `grok-imagine-image` | Default; ~5–10 s |
| Image | `grok-imagine-image-quality` | Higher fidelity; ~10–20 s |
| Video | `grok-imagine-video` | Text-to-video |
| Video | `grok-imagine-video-1.5-preview` | Image-to-video; dated alias `grok-imagine-video-1.5-2026-05-30` |
| TTS | (default voice) | xAI `/v1/tts` endpoint |

The chat catalog is derived live from the on-disk `models.dev` cache; new xAI releases appear automatically once that cache refreshes. `grok-4.3` is always pinned to the top of the list.

## Environment Variables

| Variable | Effect |
|----------|--------|
| `XAI_BASE_URL` | Override the default `https://api.x.ai/v1` endpoint (rarely needed). |

To select xAI as the active provider, set `model.provider: xai-oauth` in `config.yaml` (use `janus setup` for the guided flow) or pass `--provider xai-oauth` for a single invocation.

## Troubleshooting

### Token expired — not re-logging in automatically

Janus refreshes the token before each session and again reactively on a 401. If refresh fails with `invalid_grant` (the refresh token was revoked, or the account was rotated), Janus surfaces a typed re-auth message instead of crashing.

When the refresh failure is terminal (HTTP 4xx, `invalid_grant`, revoked grant, etc.), Janus marks the refresh token as dead and quarantines it locally — subsequent calls skip the doomed refresh attempt instead of replaying the same 401 over and over. The agent surfaces a single "re-authentication required" message and stays out of the way until you log in again.

**Fix:** run `janus auth add xai-oauth` again to start a fresh login. The quarantine clears on the next successful exchange.

### Authorization timed out

The loopback listener has a finite expiry window (default 180 s). If you don't approve the login in time, Janus raises a timeout error.

**Fix:** re-run `janus auth add xai-oauth` (or `janus model`). The flow starts fresh.

### State mismatch (possible CSRF)

Janus detected that the `state` value returned by the authorization server doesn't match what it sent.

**Fix:** re-run the login. If it persists, check for a proxy or redirect that is modifying the OAuth response.

### Logging in from a remote server

On SSH or container sessions Janus prints the authorization URL instead of opening a browser. The loopback callback listener still binds `127.0.0.1:56121` on the remote host — your laptop's browser can't reach it without an SSH local-forward:

```bash
# Local machine, separate terminal:
ssh -N -L 56121:127.0.0.1:56121 user@remote-host

# Remote machine:
janus auth add xai-oauth --no-browser
```

Full walkthrough (jump boxes, mosh/tmux, port conflicts): [OAuth over SSH / Remote Hosts](./oauth-over-ssh.md).

### HTTP 403 after a successful login (tier / entitlement)

OAuth completed in the browser, tokens are saved, but inference or token refresh returns `HTTP 403` with a message similar to *"The caller does not have permission to execute the specified operation"*.

This is **not** a stale-token problem — re-running `janus model` won't change it. xAI's backend has been seen to restrict OAuth API access to specific SuperGrok tiers despite the in-app subscription being active (issue [#26847](https://github.com/magnetoid/janus/issues/26847)).

**Fix:** set `XAI_API_KEY` and switch to the API-key path:

```bash
export XAI_API_KEY=xai-...
janus config set model.provider xai
```

Or upgrade your subscription at [x.ai/grok](https://x.ai/grok) if the OAuth route is required.

### "No xAI credentials found" error at runtime

The auth store has no `xai-oauth` entry and no `XAI_API_KEY` is set. You haven't logged in yet, or the credential file was deleted.

**Fix:** run `janus model` and pick the xAI Grok OAuth provider, or run `janus auth add xai-oauth`.

## Logging Out

To remove all stored xAI Grok OAuth credentials:

```bash
janus auth logout xai-oauth
```

This clears both the singleton OAuth entry in `auth.json` and any credential-pool rows for `xai-oauth`. Use `janus auth remove xai-oauth <index|id|label>` if you only want to drop a single pool entry (run `janus auth list xai-oauth` to see them).

## See Also

- [OAuth over SSH / Remote Hosts](./oauth-over-ssh.md) — required reading if Janus is on a different machine than your browser
- [AI Providers reference](../integrations/providers.md)
- [Environment Variables](../reference/environment-variables.md)
- [Configuration](../user-guide/configuration.md)
- [Voice & TTS](../user-guide/features/tts.md)
