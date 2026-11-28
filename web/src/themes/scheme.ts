import type { DashboardTheme, ThemeMode, ThemeScheme } from "./types";

/**
 * Light and dark looks.
 *
 * Pure functions, no DOM: `ThemeProvider` uses them to decide which look
 * to paint, and the tests in `web/tests/` use them to hold every built-in
 * theme to the same contrast floor in both looks.
 */

/** Relative luminance (0–1) of a `#rgb` / `#rrggbb` color; 0 when the
 *  value is not a hex color. */
export function hexLuminance(hex: string): number {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return 0;
  const h = m[1].length === 3 ? m[1].replace(/./g, "$&$&") : m[1];
  const channel = (i: number) => {
    const v = parseInt(h.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}

/** WCAG contrast ratio between two hex colors, from 1 (none) to 21. */
export function contrastRatio(a: string, b: string): number {
  const la = hexLuminance(a);
  const lb = hexLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** A canvas brighter than this is a light look. Any real canvas is far
 *  from the line: dark ones sit below 0.05, light ones above 0.8. */
const LIGHT_CANVAS_LUMINANCE = 0.4;

/** The look a theme (or a resolved look of one) paints. */
export function themeScheme(theme: Pick<DashboardTheme, "palette">): ThemeScheme {
  return hexLuminance(theme.palette.background.hex) > LIGHT_CANVAS_LUMINANCE
    ? "light"
    : "dark";
}

function otherScheme(scheme: ThemeScheme): ThemeScheme {
  return scheme === "light" ? "dark" : "light";
}

/** The looks a theme can paint: its own, plus the other one when it
 *  carries it. A variant declared for the scheme the theme already is
 *  would never be reached, so it does not count. */
export function themeSchemes(theme: DashboardTheme): ThemeScheme[] {
  const own = themeScheme(theme);
  const other = otherScheme(own);
  return theme[other] ? [own, other] : [own];
}

/** Turn the person's choice into a look. */
export function resolveScheme(mode: ThemeMode, systemPrefersDark: boolean): ThemeScheme {
  if (mode === "system") return systemPrefersDark ? "dark" : "light";
  return mode;
}

export function isThemeMode(value: unknown): value is ThemeMode {
  return value === "light" || value === "dark" || value === "system";
}

/**
 * The theme as it should be painted for `scheme`: a plain single-look
 * theme. A theme that lacks the requested look is returned in the look it
 * has — a hand-written theme is never recolored behind its author's back.
 */
export function themeForScheme(theme: DashboardTheme, scheme: ThemeScheme): DashboardTheme {
  const { light: _light, dark: _dark, ...base } = theme;
  const variant = scheme === themeScheme(theme) ? undefined : theme[scheme];
  if (!variant) return base;
  return {
    ...base,
    palette: { ...base.palette, ...variant.palette },
    colorOverrides: variant.colorOverrides,
    seriesColors: variant.seriesColors,
    swatchColors: variant.swatchColors,
    terminalBackground: variant.terminalBackground ?? base.terminalBackground,
  };
}
