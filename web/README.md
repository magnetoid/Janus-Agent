# Janus Agent — Web UI

Browser-based dashboard for managing Janus Agent configuration, API keys, and monitoring active sessions.

## Stack

- **Vite** + **React 19** + **TypeScript**
- **Tailwind CSS v4**, tokens in `src/index.css`
- **shadcn/ui** components on **Radix** primitives (`radix-ui`) and **cmdk**, copied into `src/components/ui/` — the code lives in this repo, there is no component-library dependency to upgrade

## Development

```bash
# Start the backend API server
cd ../
python -m janus_cli.main web --no-open

# In another terminal, start the Vite dev server (with HMR + API proxy)
cd web/
npm install
npm run dev
```

Open the **Vite URL** printed in the terminal (usually `http://localhost:5173`). That is the live-reload UI.

`janus dashboard` on port 9119 serves the **built** bundle from `janus_cli/web_dist/`, not the Vite dev server — changes in `web/src/` will not appear there until you run `npm run build` and restart the dashboard (or use `web --no-open` + Vite as above).

The Vite dev server proxies `/api` requests to `http://127.0.0.1:9119` (the FastAPI backend).

## Build

```bash
npm run build
```

This outputs to `../janus_cli/web_dist/`, which the FastAPI server serves as a static SPA. The built assets are included in the Python package via `pyproject.toml` package-data.

## Tests

```bash
npm test
```

Runs the files in `tests/` with Node's built-in test runner (Node 24 or newer, which runs TypeScript directly) — no test dependency to install. They cover logic that has no DOM, such as picking a theme's light or dark look and holding every built-in theme to a contrast floor in both. The modules under test must therefore import nothing but types from other files.

## Structure

```
src/
├── components/
│   ├── ui/          # Primitives: Button, Card, Badge, Input, Dialog, DropdownMenu,
│   │                # Tooltip, Sheet, Command, Skeleton, EmptyState, …
│   └── shell/       # App frame: AppSidebar, CommandMenu, BrandMark, nav model
├── contexts/        # PageHeaderProvider (top bar + page slots), SystemActions
├── lib/
│   ├── api.ts       # API client — typed fetch wrappers for all backend endpoints
│   └── utils.ts     # cn() helper for Tailwind class merging
├── pages/           # One file per page
├── plugins/         # Plugin SDK, registry and shell slots
├── themes/          # Theme model, built-in presets, light/dark looks, ThemeProvider
├── App.tsx          # Routes and layout
├── main.tsx         # React entry point
└── index.css        # Design tokens, base styles, utilities
tests/               # `npm test` — DOM-free logic
```

## Design system

Read this before adding or editing UI. The default theme is **Bronze**: a basalt canvas, bone-colored text, brass for the one thing to act on and verdigris for whatever is live. By daylight it turns over: a pale green-grey canvas, basalt text, and the brass deepened to cast bronze. Every other theme (built-in or a user's YAML) flows through the same tokens, so write against the tokens and every theme, in both of its looks, follows.

### Tokens

A theme supplies two opaque colors — `background` (the canvas) and `midground` (text) — and `src/index.css` mixes every surface, border and text step from them. Use the semantic names; never a raw hex, and never a theme layer (`midground`, `background-base`) directly.

| Use | Classes |
|-----|---------|
| Text | `text-foreground` (body), `text-text-secondary` (meta, inactive), `text-text-tertiary` (small labels, counts), `text-text-disabled` |
| Surfaces | `bg-background` (page), `bg-card` (panels), `bg-popover` (menus, dialogs — one step above a card), `bg-muted` (wells), `bg-accent` (hover and selected rows), `bg-sidebar` |
| Lines | `border-border`, `border-border-strong` (hover, emphasis), `border-input` (form fields) |
| Meaning | `primary` (brass), `success` (verdigris), `warning`, `destructive` — as `text-*`, `bg-*`, or tinted `bg-*/15` |
| Shape | `rounded-md` for controls and chips, `rounded-lg` for boxes, `rounded-xl` for cards and dialogs; `shadow-raised` only on things that float |

The steps are opaque on purpose. Do not stack alpha on text (`text-muted-foreground/60`) or use `opacity-*` below 0.7 on anything meant to be read — reach for the next text step instead.

### Light and dark

Every built-in theme has a light and a dark look. The control in the bottom-left corner of the sidebar (`ModeSwitcher`) picks light, dark, or "follow the system"; the command menu has the same three entries. The choice is kept per browser (`localStorage`, `janus-dashboard-mode`) and starts as dark.

- A theme is authored for one canvas and carries the other look under `light` (or `dark`) in `themes/presets.ts`: a canvas, a text color, and whatever `colorOverrides` that canvas needs. Colors are **not** inherited between looks — an accent tuned for a dark canvas is unreadable on a light one. Type, spacing and assets are shared.
- `src/index.css` holds the token set twice: `:root` for a dark canvas and `:root:not(.dark)` for a light one, where raised surfaces move toward white instead of toward the text color and the status colors deepen. `ThemeProvider` puts `.dark` on `<html>` from the brightness of the canvas being painted, so the light block also serves a hand-written light theme.
- Write against the tokens and both looks follow. Never `text-white`, `bg-black/…`, a hex, or a Tailwind palette shade for meaning (`text-emerald-400`, `text-amber-500`) — those are right on one canvas only. Use `text-success` / `text-warning` / `text-destructive`, and `bg-scrim` behind an overlay. Where a palette shade is the point (the cache and reasoning tags on Models), give both: `text-blue-600 dark:text-blue-400`.
- The embedded terminal on `/chat` stays dark in both looks: the TUI paints with colors chosen for a dark terminal.
- A user's YAML theme has the one look it was written with; the control is shown switched off for it, with the reason.
- `npm test` fails if a built-in theme loses a look, or if text, the primary color, a primary button label or a status color drops below its contrast floor in either look. Check a new palette there before looking at it.

### Accent discipline

Brass means "this is the thing to act on". Keep it that way:

- One primary `<Button>` per view or dialog (Save, Create, Confirm). An action repeated on every row of a list — Configure, Install, Test — is `outlined` or `ghost`.
- Hover and selection use `bg-accent`, a neutral lift, not the brand color.
- A badge labels state; it is tinted, never filled.
- An "on" switch is `success`: it says something is enabled, it is not a call to action.
- Deleting from a row is `ghost destructive` (a red icon that tints on hover). The filled red button is for the confirm step of a destructive dialog.

### Type

| Role | How | Where |
|------|-----|-------|
| UI | the default (`--theme-font-sans`) | everything unless listed below |
| Display | `font-display` | the page title in the top bar and the wordmark — nowhere else |
| Technical | `font-mono-ui` | model slugs, env keys, schedules, ids, paths, log lines |

- Minimum size is `text-xs` (12px). No arbitrary `text-[10px]` / `text-[0.65rem]`.
- Sentence case everywhere. No `uppercase`, no letter-spaced labels, and no small heading stacked above another heading.
- Numbers that line up in a column or change in place get `tabular-nums`.

### Words

- Name things the way the person using the dashboard would, not the way the code does.
- A button says what happens when it is pressed ("Save changes", not "Submit"), and the toast that follows uses the same verb.
- An empty list says what belongs there and offers the action that fills it — use `<EmptyState>`.
- An error says what went wrong and what to do next; it does not apologise.

### Layout and behaviour

- The sidebar's sections and order live in `src/components/shell/nav.ts`. A new built-in page is one entry there plus a route in `App.tsx`; it then appears in the sidebar and the command menu (Cmd/Ctrl+K), and the top bar shows its name.
- A page puts its controls in the top bar with `usePageHeader()`: `setAfterTitle` for things about the page (a period switch, a count), `setEnd` for its primary action. Slots belong to the path they were set on, so the order of effects between pages does not matter.
- Modal dialogs use `<Dialog>` (focus trap, scroll lock, Escape and scrim to close). Menus use `<DropdownMenu>`, hints use `<Tooltip>`.
- Motion only answers an action (a menu opening, a sheet sliding in) and is switched off under `prefers-reduced-motion`.
- Every interactive element must be reachable by keyboard and show the focus ring.

### Plugins and themes

`Button`, `Badge`, `Card*`, `Checkbox`, `Input`, `Label`, `Select`, `Separator` and `Tabs` are handed to dashboard plugins through `window.__JANUS_PLUGIN_SDK__`, so their props are a public contract (`plugins/sdk.d.ts`) — restyle them freely, but do not rename or remove a prop. Plugin stylesheets also read the `--color-*`, `--radius-*` and `--font-*` custom properties; `@theme inline static` in `index.css` is what keeps all of them emitted.
