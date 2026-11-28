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
├── themes/          # Theme model, built-in presets, ThemeProvider
├── App.tsx          # Routes and layout
├── main.tsx         # React entry point
└── index.css        # Design tokens, base styles, utilities
```

## Design system

Read this before adding or editing UI. The default theme is **Bronze**: a basalt canvas, bone-colored text, brass for the one thing to act on and verdigris for whatever is live. Every other theme (built-in or a user's YAML) flows through the same tokens, so write against the tokens and every theme follows.

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
