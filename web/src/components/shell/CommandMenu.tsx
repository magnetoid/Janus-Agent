import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Check, Download, Monitor, Moon, Palette, RotateCw, Sun } from "lucide-react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import type { SystemAction } from "@/contexts/system-actions-context";
import { useSystemActions } from "@/contexts/useSystemActions";
import { useI18n } from "@/i18n";
import { useTheme } from "@/themes";
import type { ThemeMode } from "@/themes";

import { navLabel, type NavGroup } from "./nav";
import { isApplePlatform } from "./shortcut";

const MODE_ITEMS = [
  { mode: "light", icon: Sun },
  { mode: "dark", icon: Moon },
  { mode: "system", icon: Monitor },
] as const satisfies ReadonlyArray<{ icon: typeof Sun; mode: ThemeMode }>;

interface CommandMenuProps {
  groups: NavGroup[];
  onOpenChange: (open: boolean) => void;
  open: boolean;
}

/**
 * Search-and-jump palette: every page, the system actions, light or dark
 * and the theme list, filtered as you type. Opens with Cmd+K (Ctrl+K
 * elsewhere).
 */
export function CommandMenu({ groups, onOpenChange, open }: CommandMenuProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { isBusy, runAction } = useSystemActions();
  const { availableThemes, mode, schemes, setMode, setTheme, themeName } = useTheme();

  useEffect(() => {
    const apple = isApplePlatform();
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== "k") return;
      if (!(apple ? e.metaKey : e.ctrlKey) || e.altKey || e.shiftKey) return;
      // Off Apple platforms Ctrl+K is "kill line" in a shell; leave it to
      // the embedded terminal when the terminal has focus.
      if (!apple && (e.target as HTMLElement | null)?.closest?.(".xterm")) return;
      e.preventDefault();
      onOpenChange(!open);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onOpenChange, open]);

  const go = (path: string) => {
    onOpenChange(false);
    navigate(path);
  };

  const run = (action: SystemAction) => {
    onOpenChange(false);
    void runAction(action);
    // The action's log is shown on the Sessions page.
    navigate("/sessions");
  };

  const pages = groups.flatMap((g) =>
    g.items.map((item) => ({ item, section: g.heading })),
  );

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title={t.app.commandMenu.open}>
      <CommandInput placeholder={t.app.commandMenu.placeholder} />
      <CommandList>
        <CommandEmpty>{t.app.commandMenu.noResults}</CommandEmpty>

        <CommandGroup heading={t.app.commandMenu.goTo}>
          {pages.map(({ item, section }) => {
            const label = navLabel(item, t);
            const Icon = item.icon;
            return (
              <CommandItem
                key={item.path}
                // The English name stays searchable in every language.
                value={`${label} ${item.label} ${item.path}`}
                onSelect={() => go(item.path)}
              >
                <Icon className="text-text-tertiary" />
                <span className="truncate text-foreground">{label}</span>
                {section && (
                  <span className="ml-auto pl-3 text-xs text-text-tertiary">
                    {section}
                  </span>
                )}
              </CommandItem>
            );
          })}
        </CommandGroup>

        <CommandGroup heading={t.app.commandMenu.actions}>
          <CommandItem disabled={isBusy} onSelect={() => run("restart")}>
            <RotateCw className="text-text-tertiary" />
            <span className="text-foreground">{t.status.restartGateway}</span>
          </CommandItem>
          <CommandItem disabled={isBusy} onSelect={() => run("update")}>
            <Download className="text-text-tertiary" />
            <span className="text-foreground">{t.status.updateJanus}</span>
          </CommandItem>
        </CommandGroup>

        <CommandGroup heading={t.theme.mode.title}>
          {MODE_ITEMS.map(({ icon: Icon, mode: m }) => (
            <CommandItem
              key={m}
              // The English word stays searchable in every language.
              value={`${t.theme.mode.title} ${t.theme.mode[m]} ${m}`}
              // A theme with one look has nothing to switch.
              disabled={schemes.length < 2}
              // Stays open, like the themes below: looks are compared.
              onSelect={() => setMode(m)}
            >
              <Icon className="text-text-tertiary" />
              <span className="truncate text-foreground">{t.theme.mode[m]}</span>
              {m === mode && <Check className="ml-auto text-primary" />}
            </CommandItem>
          ))}
        </CommandGroup>

        <CommandGroup heading={t.theme?.title ?? "Theme"}>
          {availableThemes.map((th) => (
            <CommandItem
              key={th.name}
              value={`${t.theme?.title ?? "Theme"} ${th.label} ${th.name}`}
              // Stays open: themes are tried one after another.
              onSelect={() => setTheme(th.name)}
            >
              <Palette className="text-text-tertiary" />
              <span className="truncate text-foreground">{th.label}</span>
              {th.name === themeName && <Check className="ml-auto text-primary" />}
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
