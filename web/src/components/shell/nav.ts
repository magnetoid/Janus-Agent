import type { ComponentType } from "react";
import {
  Activity,
  BarChart3,
  BookOpen,
  Brain,
  Clock,
  Code,
  Cpu,
  Database,
  Eye,
  FileText,
  Globe,
  Heart,
  KeyRound,
  MessageSquare,
  Package,
  Plug,
  Puzzle,
  Radio,
  Settings,
  Shield,
  ShieldCheck,
  Sparkles,
  Star,
  Terminal,
  Users,
  Webhook,
  Wrench,
  Zap,
} from "lucide-react";

import type { Translations } from "@/i18n/types";
import type { PluginManifest } from "@/plugins";

export type NavIcon = ComponentType<{ className?: string }>;

/** Which sidebar section a built-in page sits in. `main` has no heading. */
export type NavGroupId = "main" | "automation" | "connections" | "setup" | "help";

export interface NavItem {
  group?: NavGroupId;
  icon: NavIcon;
  label: string;
  /** Key into `t.app.nav` when the label is translated. */
  labelKey?: string;
  path: string;
}

export interface NavGroup {
  heading?: string;
  id: string;
  items: NavItem[];
}

export const CHAT_NAV_ITEM: NavItem = {
  group: "main",
  path: "/chat",
  labelKey: "chat",
  label: "Chat",
  icon: Terminal,
};

/**
 * Built-in pages except /chat, in sidebar order. Grouped by what you do
 * there: watch the agent work, set up what it does on its own, connect
 * it to the outside, and configure it.
 */
export const BUILTIN_NAV: NavItem[] = [
  { group: "main", path: "/sessions", labelKey: "sessions", label: "Sessions", icon: MessageSquare },
  { group: "main", path: "/analytics", labelKey: "analytics", label: "Analytics", icon: BarChart3 },
  { group: "main", path: "/logs", labelKey: "logs", label: "Logs", icon: FileText },

  { group: "automation", path: "/cron", labelKey: "cron", label: "Cron", icon: Clock },
  { group: "automation", path: "/skills", labelKey: "skills", label: "Skills", icon: Package },
  { group: "automation", path: "/learning", label: "Learning", icon: Brain },
  { group: "automation", path: "/webhooks", label: "Webhooks", icon: Webhook },

  { group: "connections", path: "/channels", label: "Channels", icon: Radio },
  { group: "connections", path: "/pairing", label: "Pairing", icon: ShieldCheck },
  { group: "connections", path: "/mcp", label: "MCP", icon: Plug },
  { group: "connections", path: "/plugins", labelKey: "plugins", label: "Plugins", icon: Puzzle },

  { group: "setup", path: "/models", labelKey: "models", label: "Models", icon: Cpu },
  { group: "setup", path: "/profiles", labelKey: "profiles", label: "Profiles", icon: Users },
  { group: "setup", path: "/config", labelKey: "config", label: "Config", icon: Settings },
  { group: "setup", path: "/env", labelKey: "keys", label: "Keys", icon: KeyRound },
  { group: "setup", path: "/system", label: "System", icon: Wrench },

  { group: "help", path: "/docs", labelKey: "documentation", label: "Documentation", icon: BookOpen },
];

/** Icons a plugin manifest may name for its tab. */
const PLUGIN_ICONS: Record<string, NavIcon> = {
  Activity,
  BarChart3,
  Clock,
  Cpu,
  FileText,
  KeyRound,
  MessageSquare,
  Package,
  Settings,
  Puzzle,
  Sparkles,
  Terminal,
  Globe,
  Database,
  Shield,
  Users,
  Wrench,
  Zap,
  Heart,
  Star,
  Code,
  Eye,
};

export function navLabel(item: NavItem, t: Translations): string {
  if (!item.labelKey) return item.label;
  return (t.app.nav as Record<string, string>)[item.labelKey] ?? item.label;
}

/** Built-in items followed by plugin tabs, honouring each plugin's
 *  `after:<page>` / `before:<page>` position hint. */
function mergePluginTabs(builtIn: NavItem[], manifests: PluginManifest[]): NavItem[] {
  const items = [...builtIn];

  for (const manifest of manifests) {
    if (manifest.tab.override) continue;
    if (manifest.tab.hidden) continue;

    const pluginItem: NavItem = {
      path: manifest.tab.path,
      label: manifest.label,
      icon: PLUGIN_ICONS[manifest.icon] ?? Puzzle,
    };

    const pos = manifest.tab.position ?? "end";
    if (pos.startsWith("after:")) {
      const idx = items.findIndex((i) => i.path === "/" + pos.slice(6));
      items.splice(idx >= 0 ? idx + 1 : items.length, 0, pluginItem);
    } else if (pos.startsWith("before:")) {
      const idx = items.findIndex((i) => i.path === "/" + pos.slice(7));
      items.splice(idx >= 0 ? idx : items.length, 0, pluginItem);
    } else {
      items.push(pluginItem);
    }
  }

  return items;
}

/**
 * The sidebar's sections, top to bottom. Plugin tabs get a section of
 * their own, in the order their position hints produce, so a third-party
 * tab never lands inside a built-in group.
 */
export function buildNavGroups(
  builtIn: NavItem[],
  manifests: PluginManifest[],
  t: Translations,
): NavGroup[] {
  const builtinPaths = new Set(builtIn.map((i) => i.path));
  const pluginItems = mergePluginTabs(builtIn, manifests).filter(
    (i) => !builtinPaths.has(i.path),
  );
  const of = (id: NavGroupId) => builtIn.filter((i) => i.group === id);

  const groups: NavGroup[] = [
    { id: "main", items: of("main") },
    { id: "automation", heading: t.app.navGroups.automation, items: of("automation") },
    { id: "connections", heading: t.app.navGroups.connections, items: of("connections") },
    { id: "setup", heading: t.app.navGroups.setup, items: of("setup") },
    { id: "plugins", heading: t.app.pluginNavSection, items: pluginItems },
    { id: "help", items: of("help") },
  ];
  return groups.filter((g) => g.items.length > 0);
}
