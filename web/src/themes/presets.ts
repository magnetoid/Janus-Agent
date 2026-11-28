import type { DashboardTheme, ThemeTypography, ThemeLayout } from "./types";

/**
 * Built-in dashboard themes.
 *
 * Each theme defines its own palette, typography, and layout so switching
 * themes produces visible changes beyond just color — fonts, density, and
 * corner-radius all shift to match the theme's personality.
 *
 * Every built-in is authored on a dark canvas and carries a `light` look
 * for the sidebar's appearance control. `web/tests/theme-scheme.test.ts`
 * holds both looks of each theme to the same contrast floor.
 *
 * Theme names must stay in sync with the backend's
 * `_BUILTIN_DASHBOARD_THEMES` list in `janus_cli/web_server.py`.
 */

// ---------------------------------------------------------------------------
// Shared typography / layout presets
// ---------------------------------------------------------------------------

/** Default system stack — neutral, safe fallback for every platform. */
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';
/** JetBrains Mono ships with the dashboard (it is the terminal font), so
 *  using it for technical values costs no extra request. */
const BUNDLED_MONO = `"JetBrains Mono", ${SYSTEM_MONO}`;
/** Palatino and its metric-compatible relatives: an old-style Roman face
 *  that is preinstalled on macOS, Windows and most Linux desktops, so the
 *  page titles get a distinct voice without fetching a webfont. */
const ROMAN_SERIF =
  '"Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", P052, "URW Palladio L", Georgia, serif';

const DEFAULT_TYPOGRAPHY: ThemeTypography = {
  fontSans: SYSTEM_SANS,
  fontMono: SYSTEM_MONO,
  baseSize: "15px",
  lineHeight: "1.5",
  letterSpacing: "0",
};

const DEFAULT_LAYOUT: ThemeLayout = {
  radius: "0.5rem",
  density: "comfortable",
};

/** The corner glow is blended with `lighten`, so on a light canvas it
 *  could only ever wash the page out. Light looks switch it off. */
const NO_GLOW = "rgba(0, 0, 0, 0)";

// ---------------------------------------------------------------------------
// Themes
// ---------------------------------------------------------------------------

/**
 * Bronze — the default. Janus is the Roman god of doorways, cast in bronze
 * on the coins that carried his two faces, so the palette is aged bronze:
 * a basalt canvas with a green cast, bone-colored text, brass for the one
 * thing to act on and verdigris for whatever is live. The brass matches
 * the CLI's default gold skin.
 *
 * By daylight the same metal reads the other way round: the canvas is the
 * basalt's green-grey lifted almost to white, the text is the basalt, and
 * the brass deepens to cast bronze so it still carries a label and a link.
 *
 * Keep both looks in step with the `:root` and `:root:not(.dark)` defaults
 * in `src/index.css`, which paint the first frame before this theme is
 * applied.
 */
export const defaultTheme: DashboardTheme = {
  name: "default",
  label: "Bronze",
  description: "Basalt canvas, bone text and a brass accent",
  palette: {
    background: { hex: "#111614", alpha: 1 },
    midground: { hex: "#e9e6db", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 0, 0, 0)",
    noiseOpacity: 0,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontMono: BUNDLED_MONO,
    fontDisplay: ROMAN_SERIF,
  },
  layout: DEFAULT_LAYOUT,
  colorOverrides: {
    primary: "#d2a95c",
    primaryForeground: "#1b1508",
    ring: "#d2a95c",
  },
  seriesColors: {
    inputTokenAccent: "#d2a95c",
    outputTokenAccent: "#5cc3a3",
  },
  terminalBackground: "#0d100f",
  light: {
    palette: {
      background: { hex: "#e9eeea", alpha: 1 },
      midground: { hex: "#131917", alpha: 1 },
    },
    colorOverrides: {
      primary: "#7f570d",
      primaryForeground: "#fffaf0",
    },
    seriesColors: {
      inputTokenAccent: "#7f570d",
      outputTokenAccent: "#18705a",
    },
  },
};

export const midnightTheme: DashboardTheme = {
  name: "midnight",
  label: "Midnight",
  description: "Deep blue-violet with cool accents",
  palette: {
    background: { hex: "#0a0a1f", alpha: 1 },
    midground: { hex: "#d4c8ff", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(167, 139, 250, 0.32)",
    noiseOpacity: 0.8,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    letterSpacing: "-0.005em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.75rem",
  },
  colorOverrides: {
    primary: "#a78bfa",
    primaryForeground: "#140c33",
    ring: "#a78bfa",
  },
  light: {
    palette: {
      background: { hex: "#eeedf9", alpha: 1 },
      midground: { hex: "#17143a", alpha: 1 },
      warmGlow: NO_GLOW,
    },
    colorOverrides: {
      primary: "#5b3fc4",
      primaryForeground: "#f7f5ff",
    },
  },
};

export const emberTheme: DashboardTheme = {
  name: "ember",
  label: "Ember",
  description: "Warm crimson and bronze — forge vibes",
  palette: {
    background: { hex: "#1a0a06", alpha: 1 },
    midground: { hex: "#ffd8b0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 115, 22, 0.38)",
    noiseOpacity: 1,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.25rem",
  },
  colorOverrides: {
    primary: "#f2a25c",
    primaryForeground: "#2a1206",
    ring: "#f2a25c",
    destructive: "#f0553a",
    warning: "#f97316",
  },
  light: {
    palette: {
      background: { hex: "#f6ebe1", alpha: 1 },
      midground: { hex: "#2b130a", alpha: 1 },
      warmGlow: NO_GLOW,
    },
    colorOverrides: {
      primary: "#9a4410",
      primaryForeground: "#fff6ee",
      destructive: "#b42a17",
      warning: "#8f4a00",
    },
  },
};

export const monoTheme: DashboardTheme = {
  name: "mono",
  label: "Mono",
  description: "Clean grayscale — minimal and focused",
  palette: {
    background: { hex: "#0e0e0e", alpha: 1 },
    midground: { hex: "#eaeaea", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 255, 255, 0.1)",
    noiseOpacity: 0.6,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
  light: {
    palette: {
      background: { hex: "#f4f4f4", alpha: 1 },
      midground: { hex: "#141414", alpha: 1 },
      warmGlow: NO_GLOW,
    },
  },
};

export const cyberpunkTheme: DashboardTheme = {
  name: "cyberpunk",
  label: "Cyberpunk",
  description: "Neon green on black — matrix terminal",
  palette: {
    background: { hex: "#040608", alpha: 1 },
    midground: { hex: "#9bffcf", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 255, 136, 0.22)",
    noiseOpacity: 1.2,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
  colorOverrides: {
    success: "#00ff88",
    warning: "#ffd700",
    destructive: "#ff0055",
  },
  light: {
    palette: {
      background: { hex: "#e8f3ec", alpha: 1 },
      midground: { hex: "#03170f", alpha: 1 },
      warmGlow: NO_GLOW,
    },
    colorOverrides: {
      success: "#007040",
      warning: "#775f00",
      destructive: "#c0003c",
    },
  },
};

export const roseTheme: DashboardTheme = {
  name: "rose",
  label: "Rosé",
  description: "Soft pink and warm ivory — easy on the eyes",
  palette: {
    background: { hex: "#1a0f15", alpha: 1 },
    midground: { hex: "#ffd4e1", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 168, 212, 0.3)",
    noiseOpacity: 0.9,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Fraunces", Georgia, serif`,
    fontMono: `"DM Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "1rem",
  },
  colorOverrides: {
    primary: "#f9a8d4",
    primaryForeground: "#2b0f1d",
    ring: "#f9a8d4",
  },
  light: {
    palette: {
      background: { hex: "#fbeef2", alpha: 1 },
      midground: { hex: "#3a1426", alpha: 1 },
      warmGlow: NO_GLOW,
    },
    colorOverrides: {
      primary: "#a8356b",
      primaryForeground: "#fff5f9",
    },
  },
};

/**
 * Bronze with a larger root font size, looser line-height and `spacious`
 * density, so every rem-based size scales up. For people who find the
 * default 15px UI too dense.
 */
export const defaultLargeTheme: DashboardTheme = {
  ...defaultTheme,
  name: "default-large",
  label: "Bronze (Large)",
  description: "Bronze with bigger type and roomier spacing",
  typography: {
    ...defaultTheme.typography,
    baseSize: "18px",
    lineHeight: "1.6",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    density: "spacious",
  },
};

export const BUILTIN_THEMES: Record<string, DashboardTheme> = {
  default: defaultTheme,
  "default-large": defaultLargeTheme,
  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  rose: roseTheme,
};
