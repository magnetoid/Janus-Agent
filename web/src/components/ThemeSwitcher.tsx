import { Palette, Type } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useI18n } from "@/i18n";
import { BUILTIN_THEMES, THEME_DEFAULT_FONT_ID, useTheme } from "@/themes";
import type { DashboardTheme, FontChoice } from "@/themes";
import { cn } from "@/lib/utils";

const FONT_CATEGORIES: FontChoice["category"][] = ["sans", "serif", "mono"];

const FONT_CATEGORY_LABEL_KEY: Record<
  FontChoice["category"],
  "fontSans" | "fontSerif" | "fontMono"
> = {
  sans: "fontSans",
  serif: "fontSerif",
  mono: "fontMono",
};

/**
 * Theme and font picker. Each theme row shows a three-stop swatch of its
 * palette; user themes from `<janus home>/dashboard-themes/*.yaml` carry
 * their own definition, so they preview the same way as built-ins.
 *
 * The font is a separate choice that survives theme switches; it lives in
 * a submenu because the catalog is long and rarely visited.
 */
export function ThemeSwitcher({ collapsed = false }: ThemeSwitcherProps) {
  const { themeName, availableThemes, setTheme, fontId, fontChoices, setFont } =
    useTheme();
  const { t } = useI18n();

  const current = availableThemes.find((th) => th.name === themeName);
  const label = current?.label ?? themeName;
  const switchLabel = t.theme?.switchTheme ?? "Switch theme";
  const activeFont = fontChoices.find((f) => f.id === fontId);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          ghost
          size={collapsed ? "icon" : "sm"}
          aria-label={`${switchLabel}: ${label}`}
          title={collapsed ? undefined : switchLabel}
          className={cn(
            "text-text-secondary hover:text-foreground",
            !collapsed && "min-w-0 justify-start px-2 font-normal",
          )}
        >
          <Palette />
          {!collapsed && <span className="truncate">{label}</span>}
        </Button>
      </DropdownMenuTrigger>

      <DropdownMenuContent side="top" align="start" className="w-72">
        <DropdownMenuLabel>{t.theme?.title ?? "Theme"}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={themeName} onValueChange={setTheme}>
          {availableThemes.map((th) => {
            const palette = BUILTIN_THEMES[th.name] ?? th.definition;
            return (
              <DropdownMenuRadioItem key={th.name} value={th.name} className="items-start gap-2.5">
                <ThemeSwatch theme={palette} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-foreground">{th.label}</span>
                  {th.description && (
                    <span className="truncate text-xs text-text-tertiary">
                      {th.description}
                    </span>
                  )}
                </span>
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>

        <DropdownMenuSeparator />

        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <Type className="text-text-tertiary" />
            <span>{t.theme?.fontTitle ?? "Font"}</span>
            <span className="ml-auto truncate pl-3 text-xs text-text-tertiary">
              {activeFont?.label ?? t.theme?.fontDefault ?? "Theme default"}
            </span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="w-60">
            <DropdownMenuRadioGroup value={fontId} onValueChange={setFont}>
              {/* Picking a font keeps the menu open so several can be tried. */}
              <DropdownMenuRadioItem
                value={THEME_DEFAULT_FONT_ID}
                onSelect={(e) => e.preventDefault()}
                className="items-start"
              >
                <span className="flex min-w-0 flex-col">
                  <span className="text-foreground">
                    {t.theme?.fontDefault ?? "Theme default"}
                  </span>
                  <span className="text-xs text-text-tertiary">
                    {t.theme?.fontDefaultHint ?? "Use the active theme's font"}
                  </span>
                </span>
              </DropdownMenuRadioItem>

              {FONT_CATEGORIES.map((category) => {
                const fonts = fontChoices.filter((f) => f.category === category);
                if (fonts.length === 0) return null;
                return (
                  <div key={category}>
                    <DropdownMenuLabel className="pt-2.5">
                      {t.theme?.[FONT_CATEGORY_LABEL_KEY[category]] ?? category}
                    </DropdownMenuLabel>
                    {fonts.map((f) => (
                      <DropdownMenuRadioItem
                        key={f.id}
                        value={f.id}
                        onSelect={(e) => e.preventDefault()}
                      >
                        {/* Each font previews itself. */}
                        <span className="truncate" style={{ fontFamily: f.stack }}>
                          {f.label}
                        </span>
                      </DropdownMenuRadioItem>
                    ))}
                  </div>
                );
              })}
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ThemeSwatch({ theme }: { theme: DashboardTheme | undefined }) {
  if (!theme) {
    return (
      <span
        aria-hidden
        className="mt-0.5 h-4 w-9 shrink-0 rounded-[4px] border border-dashed border-border-strong"
      />
    );
  }
  // Inverted ("lens") themes author their palette pre-inversion, so the raw
  // hexes are not what is on screen. Those themes supply `swatchColors`.
  const [canvas, ink, accent] = theme.swatchColors ?? [
    theme.palette.background.hex,
    theme.palette.midground.hex,
    theme.colorOverrides?.primary ?? theme.palette.midground.hex,
  ];
  return (
    <span
      aria-hidden
      className="mt-0.5 flex h-4 w-9 shrink-0 overflow-hidden rounded-[4px] border border-border-strong"
    >
      <span className="flex-[2]" style={{ background: canvas }} />
      <span className="flex-1" style={{ background: ink }} />
      <span className="flex-1" style={{ background: accent }} />
    </span>
  );
}

interface ThemeSwitcherProps {
  /** Icon-only trigger, for the collapsed sidebar rail. */
  collapsed?: boolean;
}
