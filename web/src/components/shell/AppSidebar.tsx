import type { ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { Download, MoreHorizontal, RotateCw, Search } from "lucide-react";

import { AuthWidget } from "@/components/AuthWidget";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { ThemeSwitcher } from "@/components/ThemeSwitcher";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Kbd } from "@/components/ui/kbd";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { SystemAction } from "@/contexts/system-actions-context";
import { useSystemActions } from "@/contexts/useSystemActions";
import { useI18n } from "@/i18n";
import type { Translations } from "@/i18n/types";
import type { StatusResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import { PluginSlot } from "@/plugins";

import { BrandMark } from "./BrandMark";
import { navLabel, type NavGroup, type NavItem } from "./nav";
import { commandShortcutLabel } from "./shortcut";

interface AppSidebarProps {
  /** Icon-only rail (desktop). Labels move into tooltips. */
  collapsed?: boolean;
  groups: NavGroup[];
  /** Called after a navigation, so the mobile sheet can close. */
  onNavigate?: () => void;
  onOpenCommand: () => void;
  status: StatusResponse | null;
}

/**
 * The navigation column: brand, search, grouped pages, then what the
 * system is doing and the personal settings. Rendered as a fixed column
 * on wide screens and inside a sheet on narrow ones.
 */
export function AppSidebar({
  collapsed = false,
  groups,
  onNavigate,
  onOpenCommand,
  status,
}: AppSidebarProps) {
  const { t } = useI18n();

  return (
    <div className="flex h-full min-h-0 flex-col text-sidebar-foreground">
      <div
        className={cn(
          "flex h-[var(--topbar-height)] shrink-0 items-center gap-2.5",
          collapsed ? "justify-center" : "px-4",
        )}
      >
        {!collapsed && <PluginSlot name="header-left" />}
        <Link
          to="/"
          onClick={onNavigate}
          aria-label={t.app.brand}
          className="flex min-w-0 items-center gap-2.5 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <BrandMark className="text-primary" />
          {!collapsed && (
            <span className="truncate font-display text-[1.1875rem] leading-none text-foreground">
              Janus
            </span>
          )}
        </Link>
      </div>

      <div className={cn("shrink-0 pb-2", collapsed ? "px-2.5" : "px-3")}>
        <RailTooltip collapsed={collapsed} label={t.app.commandMenu.open}>
          <button
            type="button"
            onClick={onOpenCommand}
            aria-label={collapsed ? t.app.commandMenu.open : undefined}
            className={cn(
              "flex h-8 w-full cursor-pointer items-center gap-2 rounded-md border border-sidebar-border bg-background/40 text-[0.8125rem] text-text-tertiary outline-none transition-colors hover:border-border-strong hover:text-text-secondary focus-visible:ring-2 focus-visible:ring-ring",
              collapsed ? "justify-center" : "px-2.5",
            )}
          >
            <Search className="size-3.5 shrink-0" />
            {!collapsed && (
              <>
                <span className="flex-1 truncate text-left">
                  {t.app.commandMenu.open}
                </span>
                <Kbd>{commandShortcutLabel()}</Kbd>
              </>
            )}
          </button>
        </RailTooltip>
      </div>

      <nav
        aria-label={t.app.navigation}
        className={cn(
          "min-h-0 flex-1 overflow-y-auto overflow-x-hidden pb-3",
          collapsed ? "px-2.5" : "px-3",
        )}
      >
        {groups.map((group, index) => (
          <div
            key={group.id}
            role="group"
            aria-label={group.heading}
            className={cn(
              // The last group (help) is pushed apart from the page list.
              index > 0 && (group.heading || collapsed ? "mt-3" : "mt-3 border-t border-sidebar-border pt-2"),
            )}
          >
            {group.heading &&
              (collapsed ? (
                <div aria-hidden className="mx-1 mb-2 h-px bg-sidebar-border" />
              ) : (
                <div className="px-2.5 pb-1 text-xs text-text-tertiary">
                  {group.heading}
                </div>
              ))}
            <ul className="flex flex-col gap-px">
              {group.items.map((item) => (
                <li key={item.path}>
                  <NavRow
                    collapsed={collapsed}
                    item={item}
                    onNavigate={onNavigate}
                    t={t}
                  />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      <div
        className={cn(
          "flex shrink-0 flex-col gap-1 border-t border-sidebar-border py-2",
          collapsed ? "items-center px-2.5" : "px-3",
        )}
      >
        <SystemStatus collapsed={collapsed} onNavigate={onNavigate} status={status} />

        <div
          className={cn(
            "flex min-w-0 items-center gap-0.5",
            collapsed && "flex-col",
          )}
        >
          {!collapsed && <PluginSlot name="header-right" />}
          <RailTooltip collapsed={collapsed} label={t.theme?.switchTheme ?? "Switch theme"}>
            <ThemeSwitcher collapsed={collapsed} />
          </RailTooltip>
          <RailTooltip collapsed={collapsed} label={t.language.switchTo}>
            <LanguageSwitcher collapsed={collapsed} />
          </RailTooltip>
        </div>

        {!collapsed && (
          <>
            <AuthWidget />
            <div className="flex items-center justify-between gap-2 px-2.5 pb-0.5 pt-1 text-xs text-text-tertiary">
              <span className="font-mono-ui tabular-nums">
                {status?.version != null ? `v${status.version}` : ""}
              </span>
              <a
                href="https://cloud-industry.com"
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-sm outline-none transition-colors hover:text-text-secondary focus-visible:ring-2 focus-visible:ring-ring"
              >
                {t.app.footer.org}
              </a>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** Wraps a rail control in a tooltip naming it; a no-op when expanded,
 *  where the control already shows its label. */
function RailTooltip({
  children,
  collapsed,
  label,
}: {
  children: ReactNode;
  collapsed: boolean;
  label: string;
}) {
  if (!collapsed) return <>{children}</>;
  return (
    <Tooltip>
      {/* A span, not `asChild`: some children are themselves menu triggers
          and two Radix triggers cannot share one element. */}
      <TooltipTrigger asChild>
        <span className="flex w-full justify-center">{children}</span>
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

function NavRow({
  collapsed,
  item,
  onNavigate,
  t,
}: {
  collapsed: boolean;
  item: NavItem;
  onNavigate?: () => void;
  t: Translations;
}) {
  const label = navLabel(item, t);
  const Icon = item.icon;

  const link = (
    <NavLink
      to={item.path}
      end={item.path === "/sessions"}
      onClick={onNavigate}
      aria-label={collapsed ? label : undefined}
      className={({ isActive }) =>
        cn(
          "group/nav relative flex h-7 items-center gap-2.5 rounded-md text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
          collapsed ? "justify-center" : "px-2.5",
          isActive
            ? "bg-accent font-medium text-foreground"
            : "hover:bg-accent/60 hover:text-foreground",
        )
      }
      style={{ clipPath: "var(--component-tab-clip-path)" }}
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <span
              aria-hidden
              className="absolute inset-y-1 -left-1.5 w-0.5 rounded-full bg-primary"
            />
          )}
          <Icon
            className={cn(
              "size-4 shrink-0 transition-colors",
              isActive
                ? "text-primary"
                : "text-text-tertiary group-hover/nav:text-text-secondary",
            )}
          />
          {!collapsed && <span className="truncate">{label}</span>}
        </>
      )}
    </NavLink>
  );

  if (!collapsed) return link;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

/** How the gateway is doing, in the label and tone the whole UI uses. */
// eslint-disable-next-line react-refresh/only-export-components -- shared helper
export function gatewayLine(
  status: StatusResponse,
  t: Translations,
): { dot: string; label: string; tone: string } {
  const g = t.app.gatewayStrip;
  const byState: Record<string, { dot: string; label: string; tone: string }> = {
    running: { dot: "bg-success", label: g.running, tone: "text-success" },
    starting: { dot: "bg-warning", label: g.starting, tone: "text-warning" },
    startup_failed: { dot: "bg-destructive", label: g.failed, tone: "text-destructive" },
    stopped: { dot: "bg-text-disabled", label: g.stopped, tone: "text-text-secondary" },
  };
  if (status.gateway_state && byState[status.gateway_state]) {
    return byState[status.gateway_state];
  }
  return status.gateway_running
    ? byState.running
    : { dot: "bg-text-disabled", label: g.off, tone: "text-text-secondary" };
}

/**
 * Gateway state and the count of active sessions, with the two system
 * actions (restart the gateway, update Janus) behind a menu. While an
 * action runs, the row says so in place of the gateway state.
 */
function SystemStatus({
  collapsed,
  onNavigate,
  status,
}: {
  collapsed: boolean;
  onNavigate?: () => void;
  status: StatusResponse | null;
}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { activeAction, isBusy, isRunning, pendingAction, runAction } =
    useSystemActions();

  const run = (action: SystemAction) => {
    if (isBusy) return;
    void runAction(action);
    // The action's log is shown on the Sessions page.
    navigate("/sessions");
    onNavigate?.();
  };

  const running: SystemAction | null =
    pendingAction ?? (isRunning ? activeAction : null);
  const runningLabel =
    running === "restart"
      ? t.status.restartingGateway
      : running === "update"
        ? t.status.updatingJanus
        : null;

  const gw = status ? gatewayLine(status, t) : null;
  const summary = runningLabel ?? (gw ? `${t.status.gateway}: ${gw.label}` : t.status.gateway);

  const indicator = running ? (
    <Spinner className="size-3.5 text-primary" />
  ) : (
    <span
      aria-hidden
      className={cn("size-2 rounded-full", gw?.dot ?? "bg-text-disabled")}
    />
  );

  const menu = (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          ghost
          size="icon"
          aria-label={t.app.system}
          className="size-8 shrink-0 text-text-tertiary hover:text-foreground"
        >
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align={collapsed ? "start" : "end"}>
        <DropdownMenuItem disabled={isBusy} onSelect={() => run("restart")}>
          <RotateCw className="text-text-tertiary" />
          {t.status.restartGateway}
        </DropdownMenuItem>
        <DropdownMenuItem disabled={isBusy} onSelect={() => run("update")}>
          <Download className="text-text-tertiary" />
          {t.status.updateJanus}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );

  if (collapsed) {
    return (
      <>
        <Tooltip>
          <TooltipTrigger asChild>
            <Link
              to="/sessions"
              onClick={onNavigate}
              aria-label={summary}
              className="flex size-8 items-center justify-center rounded-md outline-none transition-colors hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring"
            >
              {indicator}
            </Link>
          </TooltipTrigger>
          <TooltipContent side="right">{summary}</TooltipContent>
        </Tooltip>
        {menu}
      </>
    );
  }

  return (
    <div className="flex items-center gap-1">
      <Link
        to="/sessions"
        onClick={onNavigate}
        title={t.app.statusOverview}
        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-2.5 py-1.5 outline-none transition-colors hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="flex size-4 shrink-0 items-center justify-center">
          {indicator}
        </span>
        {status === null && !running ? (
          <Skeleton className="h-3 w-24" />
        ) : (
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="truncate text-[0.8125rem] text-foreground">
              {runningLabel ?? (
                <>
                  {t.status.gateway}{" "}
                  <span className={gw?.tone}>{gw?.label.toLowerCase()}</span>
                </>
              )}
            </span>
            {status && (
              <span className="truncate text-xs tabular-nums text-text-tertiary">
                {t.app.sessionsActiveCount.replace(
                  "{count}",
                  String(status.active_sessions),
                )}
              </span>
            )}
          </span>
        )}
      </Link>
      {menu}
    </div>
  );
}
