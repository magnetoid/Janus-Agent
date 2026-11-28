import { useRef, type KeyboardEvent } from "react";
import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { useTheme } from "@/themes";
import type { ThemeMode } from "@/themes";

const MODES: Array<{ icon: LucideIcon; mode: ThemeMode }> = [
  { mode: "light", icon: Sun },
  { mode: "dark", icon: Moon },
  { mode: "system", icon: Monitor },
];

/**
 * Light, dark, or follow the operating system. Sits in the bottom-left
 * corner of the sidebar, apart from the theme picker: the theme is which
 * palette, this is which of its two looks.
 *
 * A theme with a single look (a hand-written one, usually) has nothing to
 * switch, so the control is shown switched off and says why, rather than
 * silently doing nothing.
 */
export function ModeSwitcher({ collapsed = false }: ModeSwitcherProps) {
  const { mode, schemes, setMode } = useTheme();
  const { t } = useI18n();
  const labels = t.theme.mode;
  const fixed = schemes.length < 2;
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);

  if (collapsed) {
    const ActiveIcon = MODES.find((m) => m.mode === mode)?.icon ?? Moon;
    return (
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            ghost
            size="icon"
            aria-label={`${labels.title}: ${labels[mode]}`}
            className="text-text-secondary hover:text-foreground"
          >
            <ActiveIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" className="w-48">
          <DropdownMenuLabel>{labels.title}</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={mode}
            onValueChange={(next) => setMode(next as ThemeMode)}
          >
            {MODES.map(({ icon: Icon, mode: m }) => (
              <DropdownMenuRadioItem key={m} value={m} disabled={fixed}>
                <Icon className="text-text-tertiary" />
                {labels[m]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          {fixed && (
            <p className="px-2 pb-1.5 pt-1 text-xs text-text-tertiary">{labels.oneLook}</p>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  // A radio group moves with the arrow keys and takes one tab stop.
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const step =
      e.key === "ArrowRight" || e.key === "ArrowDown"
        ? 1
        : e.key === "ArrowLeft" || e.key === "ArrowUp"
          ? -1
          : 0;
    if (step === 0 || fixed) return;
    e.preventDefault();
    const index = MODES.findIndex((m) => m.mode === mode);
    const next = (index + step + MODES.length) % MODES.length;
    setMode(MODES[next].mode);
    buttons.current[next]?.focus();
  };

  const group = (
    <div
      role="radiogroup"
      aria-label={labels.title}
      aria-disabled={fixed || undefined}
      onKeyDown={onKeyDown}
      className={cn(
        "inline-flex shrink-0 items-center gap-0.5 rounded-lg border border-sidebar-border p-0.5",
        fixed && "opacity-55",
      )}
    >
      {MODES.map(({ icon: Icon, mode: m }, index) => {
        const active = m === mode;
        const button = (
          <button
            key={m}
            ref={(el) => {
              buttons.current[index] = el;
            }}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={labels[m]}
            disabled={fixed}
            tabIndex={active ? 0 : -1}
            onClick={() => setMode(m)}
            className={cn(
              "flex size-7 items-center justify-center rounded-md outline-none transition-colors",
              "focus-visible:ring-2 focus-visible:ring-ring",
              "enabled:cursor-pointer disabled:cursor-not-allowed",
              active
                ? "bg-accent text-foreground"
                : "text-text-tertiary enabled:hover:text-foreground",
            )}
          >
            <Icon className="size-4" />
          </button>
        );
        // The group as a whole explains itself when it is switched off.
        if (fixed) return button;
        return (
          <Tooltip key={m}>
            <TooltipTrigger asChild>{button}</TooltipTrigger>
            <TooltipContent side="top">{labels[m]}</TooltipContent>
          </Tooltip>
        );
      })}
    </div>
  );

  if (!fixed) return group;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{group}</TooltipTrigger>
      <TooltipContent side="top">{labels.oneLook}</TooltipContent>
    </Tooltip>
  );
}

interface ModeSwitcherProps {
  /** A single icon that opens a menu, for the collapsed sidebar rail. */
  collapsed?: boolean;
}
