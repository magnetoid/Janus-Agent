import { Languages } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LOCALE_META } from "@/i18n";
import type { Locale } from "@/i18n";
import { useI18n } from "@/i18n/context";

/**
 * Language picker. Lists every supported locale by its own name.
 *
 * No country flags by design: languages aren't countries, and flag
 * pairings inevitably create political mismappings (Mandarin variants are
 * no single jurisdiction, English is not GB, Portuguese is not PT).
 * Endonyms are unambiguous.
 */
export function LanguageSwitcher({ collapsed = false }: LanguageSwitcherProps) {
  const { locale, setLocale, t } = useI18n();
  const locales = Object.entries(LOCALE_META) as Array<[Locale, { name: string }]>;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          ghost
          size={collapsed ? "icon" : "sm"}
          aria-label={`${t.language.switchTo}: ${LOCALE_META[locale].name}`}
          title={collapsed ? undefined : t.language.switchTo}
          className="shrink-0 px-2 font-normal text-text-secondary hover:text-foreground"
        >
          <Languages />
          {!collapsed && <span>{locale}</span>}
        </Button>
      </DropdownMenuTrigger>

      <DropdownMenuContent side="top" align="start" className="w-48">
        <DropdownMenuRadioGroup
          value={locale}
          onValueChange={(code) => setLocale(code as Locale)}
        >
          {locales.map(([code, meta]) => (
            <DropdownMenuRadioItem key={code} value={code} lang={code}>
              {meta.name}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

interface LanguageSwitcherProps {
  /** Icon-only trigger, for the collapsed sidebar rail. */
  collapsed?: boolean;
}
