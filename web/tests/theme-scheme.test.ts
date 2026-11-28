import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { BUILTIN_THEMES } from "../src/themes/presets.ts";
import {
  contrastRatio,
  isThemeMode,
  resolveScheme,
  themeForScheme,
  themeScheme,
  themeSchemes,
} from "../src/themes/scheme.ts";
import type { DashboardTheme, ThemeScheme } from "../src/themes/types.ts";

const SCHEMES: ThemeScheme[] = ["light", "dark"];

/** A theme authored on a dark canvas, with nothing but the required fields. */
function darkTheme(extra: Partial<DashboardTheme> = {}): DashboardTheme {
  return {
    name: "probe",
    label: "Probe",
    description: "",
    palette: {
      background: { hex: "#101010", alpha: 1 },
      midground: { hex: "#f0f0f0", alpha: 1 },
      foreground: { hex: "#ffffff", alpha: 0 },
      warmGlow: "rgba(255, 0, 0, 0.3)",
      noiseOpacity: 0.7,
    },
    typography: {
      fontSans: "sans-serif",
      fontMono: "monospace",
      baseSize: "15px",
      lineHeight: "1.5",
      letterSpacing: "0",
    },
    layout: { radius: "0.5rem", density: "comfortable" },
    ...extra,
  };
}

const LIGHT_LOOK = {
  palette: {
    background: { hex: "#fafafa", alpha: 1 },
    midground: { hex: "#111111", alpha: 1 },
  },
};

describe("resolveScheme", () => {
  it("keeps an explicit choice whatever the operating system prefers", () => {
    for (const systemPrefersDark of [true, false]) {
      assert.equal(resolveScheme("light", systemPrefersDark), "light");
      assert.equal(resolveScheme("dark", systemPrefersDark), "dark");
    }
  });

  it("follows the operating system in system mode", () => {
    assert.equal(resolveScheme("system", true), "dark");
    assert.equal(resolveScheme("system", false), "light");
  });
});

describe("isThemeMode", () => {
  it("accepts the three modes and nothing else", () => {
    for (const mode of ["light", "dark", "system"]) {
      assert.equal(isThemeMode(mode), true);
    }
    for (const junk of ["", "auto", "Light", null, undefined, 1]) {
      assert.equal(isThemeMode(junk), false);
    }
  });
});

describe("themeScheme", () => {
  it("reads the look from the canvas color", () => {
    assert.equal(themeScheme(darkTheme()), "dark");
    assert.equal(
      themeScheme(darkTheme({ palette: { ...darkTheme().palette, ...LIGHT_LOOK.palette } })),
      "light",
    );
  });
});

describe("themeSchemes", () => {
  it("lists only its own look for a theme without a second one", () => {
    assert.deepEqual(themeSchemes(darkTheme()), ["dark"]);
  });

  it("lists both looks once the theme carries the other one", () => {
    assert.deepEqual(themeSchemes(darkTheme({ light: LIGHT_LOOK })).sort(), ["dark", "light"]);
  });

  it("ignores a second look declared for the scheme the theme already is", () => {
    assert.deepEqual(themeSchemes(darkTheme({ dark: LIGHT_LOOK })), ["dark"]);
  });
});

describe("themeForScheme", () => {
  it("leaves a single-look theme as it is when asked for the look it lacks", () => {
    const theme = darkTheme({ colorOverrides: { primary: "#ff00ff" } });
    const resolved = themeForScheme(theme, "light");
    assert.deepEqual(resolved.palette, theme.palette);
    assert.deepEqual(resolved.colorOverrides, theme.colorOverrides);
    assert.equal(themeScheme(resolved), "dark");
  });

  it("swaps in the other look's canvas and text", () => {
    const resolved = themeForScheme(darkTheme({ light: LIGHT_LOOK }), "light");
    assert.equal(resolved.palette.background.hex, "#fafafa");
    assert.equal(resolved.palette.midground.hex, "#111111");
    assert.equal(themeScheme(resolved), "light");
  });

  it("keeps the theme's own look when that is what was asked for", () => {
    const theme = darkTheme({ light: LIGHT_LOOK });
    assert.deepEqual(themeForScheme(theme, "dark").palette, theme.palette);
  });

  it("inherits palette fields the other look leaves out", () => {
    const resolved = themeForScheme(darkTheme({ light: LIGHT_LOOK }), "light");
    assert.deepEqual(resolved.palette.foreground, { hex: "#ffffff", alpha: 0 });
    assert.equal(resolved.palette.noiseOpacity, 0.7);
  });

  it("carries type, spacing and identity across both looks", () => {
    const theme = darkTheme({ light: LIGHT_LOOK, layoutVariant: "tiled" });
    const resolved = themeForScheme(theme, "light");
    assert.equal(resolved.name, theme.name);
    assert.equal(resolved.label, theme.label);
    assert.deepEqual(resolved.typography, theme.typography);
    assert.deepEqual(resolved.layout, theme.layout);
    assert.equal(resolved.layoutVariant, "tiled");
  });

  it("does not carry colors picked for the other canvas", () => {
    const theme = darkTheme({
      light: LIGHT_LOOK,
      colorOverrides: { primary: "#ffee88", success: "#00ff88" },
      seriesColors: { inputTokenAccent: "#ffee88" },
      swatchColors: ["#000000", "#ffffff", "#ffee88"],
    });
    const resolved = themeForScheme(theme, "light");
    assert.equal(resolved.colorOverrides, undefined);
    assert.equal(resolved.seriesColors, undefined);
    assert.equal(resolved.swatchColors, undefined);
  });

  it("uses the other look's own color overrides", () => {
    const theme = darkTheme({
      light: { ...LIGHT_LOOK, colorOverrides: { primary: "#553300" } },
      colorOverrides: { primary: "#ffee88", ring: "#ffee88" },
    });
    assert.deepEqual(themeForScheme(theme, "light").colorOverrides, { primary: "#553300" });
  });

  it("keeps the terminal color unless the other look names one", () => {
    const base = darkTheme({ light: LIGHT_LOOK, terminalBackground: "#0a0a0a" });
    assert.equal(themeForScheme(base, "light").terminalBackground, "#0a0a0a");
    const own = darkTheme({
      light: { ...LIGHT_LOOK, terminalBackground: "#ffffff" },
      terminalBackground: "#0a0a0a",
    });
    assert.equal(themeForScheme(own, "light").terminalBackground, "#ffffff");
  });

  it("ignores a second look declared for the scheme the theme already is", () => {
    const theme = darkTheme({ dark: LIGHT_LOOK });
    assert.deepEqual(themeForScheme(theme, "dark").palette, theme.palette);
  });

  it("returns a single-look theme, so a resolved look cannot be resolved again", () => {
    const theme = darkTheme({ light: LIGHT_LOOK });
    assert.deepEqual(themeSchemes(themeForScheme(theme, "light")), ["light"]);
    assert.deepEqual(themeSchemes(themeForScheme(theme, "dark")), ["dark"]);
  });
});

describe("built-in themes", () => {
  const themes = Object.values(BUILTIN_THEMES);

  it("each have a light and a dark look", () => {
    for (const theme of themes) {
      assert.deepEqual(themeSchemes(theme).sort(), ["dark", "light"], theme.name);
    }
  });

  it("show a canvas that matches the look that was asked for", () => {
    for (const theme of themes) {
      for (const scheme of SCHEMES) {
        assert.equal(themeScheme(themeForScheme(theme, scheme)), scheme, `${theme.name} ${scheme}`);
      }
    }
  });

  it("keep body text, the primary color and status colors readable in both looks", () => {
    for (const theme of themes) {
      for (const scheme of SCHEMES) {
        const look = themeForScheme(theme, scheme);
        const where = `${theme.name} ${scheme}`;
        const canvas = look.palette.background.hex;
        const ink = look.palette.midground.hex;
        // A theme that names no primary uses its text color for it, with
        // the canvas as the label (see `overrideVars`).
        const primary = look.colorOverrides?.primary ?? ink;
        const primaryLabel = look.colorOverrides?.primaryForeground ?? canvas;

        assert.ok(contrastRatio(ink, canvas) >= 7, `${where}: body text`);
        assert.ok(contrastRatio(primary, canvas) >= 4.5, `${where}: primary on the canvas`);
        assert.ok(contrastRatio(primaryLabel, primary) >= 4.5, `${where}: primary button label`);
        for (const key of ["success", "warning", "destructive"] as const) {
          const color = look.colorOverrides?.[key];
          if (color) {
            assert.ok(contrastRatio(color, canvas) >= 4.5, `${where}: ${key}`);
          }
        }
      }
    }
  });
});
