import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { Backdrop } from "@/components/Backdrop";
import { AppSidebar } from "@/components/shell/AppSidebar";
import { CommandMenu } from "@/components/shell/CommandMenu";
import {
  BUILTIN_NAV,
  CHAT_NAV_ITEM,
  buildNavGroups,
  navLabel,
} from "@/components/shell/nav";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Spinner } from "@/components/ui/spinner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { PageHeaderProvider } from "@/contexts/PageHeaderProvider";
import { useBelowBreakpoint } from "@/hooks/use-below-breakpoint";
import { useSidebarStatus } from "@/hooks/useSidebarStatus";
import { useI18n } from "@/i18n";
import { api } from "@/lib/api";
import { isDashboardEmbeddedChatEnabled } from "@/lib/dashboard-flags";
import { cn } from "@/lib/utils";
import AnalyticsPage from "@/pages/AnalyticsPage";
import ChannelsPage from "@/pages/ChannelsPage";
import ChatPage from "@/pages/ChatPage";
import ConfigPage from "@/pages/ConfigPage";
import CronPage from "@/pages/CronPage";
import DocsPage from "@/pages/DocsPage";
import EnvPage from "@/pages/EnvPage";
import LearningPage from "@/pages/LearningPage";
import LogsPage from "@/pages/LogsPage";
import McpPage from "@/pages/McpPage";
import ModelsPage from "@/pages/ModelsPage";
import PairingPage from "@/pages/PairingPage";
import PluginsPage from "@/pages/PluginsPage";
import ProfilesPage from "@/pages/ProfilesPage";
import SessionsPage from "@/pages/SessionsPage";
import SkillsPage from "@/pages/SkillsPage";
import SystemPage from "@/pages/SystemPage";
import WebhooksPage from "@/pages/WebhooksPage";
import { PluginPage, PluginSlot, usePlugins } from "@/plugins";
import type { PluginManifest } from "@/plugins";
import { useTheme } from "@/themes";

function RootRedirect() {
  return <Navigate to="/sessions" replace />;
}

function UnknownRouteFallback({ pluginsLoading }: { pluginsLoading: boolean }) {
  if (pluginsLoading) {
    // Render nothing during the plugin-load window — a spinner here would just flash.
    return null;
  }
  return <Navigate to="/sessions" replace />;
}

/**
 * Built-in routes except /chat.  Chat is rendered persistently (outside
 * <Routes>) when embedded — see the persistent chat host block rendered
 * inline near the bottom of this file — so the PTY child, WebSocket,
 * and xterm instance survive when the user visits another tab and comes
 * back.  A `display:none` toggle hides the terminal without unmounting.
 * Routing still owns the URL so /chat deep-links, browser back/forward,
 * and nav highlight keep working.
 */
const BUILTIN_ROUTES_CORE: Record<string, ComponentType> = {
  "/": RootRedirect,
  "/sessions": SessionsPage,
  "/analytics": AnalyticsPage,
  "/models": ModelsPage,
  "/logs": LogsPage,
  "/cron": CronPage,
  "/skills": SkillsPage,
  "/learning": LearningPage,
  "/plugins": PluginsPage,
  "/mcp": McpPage,
  "/pairing": PairingPage,
  "/channels": ChannelsPage,
  "/webhooks": WebhooksPage,
  "/system": SystemPage,
  "/profiles": ProfilesPage,
  "/config": ConfigPage,
  "/env": EnvPage,
  "/docs": DocsPage,
};

// Route placeholder for /chat.  The persistent ChatPage host (rendered
// outside <Routes> when embedded chat is on) paints on top; this empty
// element just claims the path so the `*` catch-all redirect doesn't
// fire when the user navigates to /chat.
function ChatRouteSink() {
  return null;
}

function buildRoutes(
  builtinRoutes: Record<string, ComponentType>,
  manifests: PluginManifest[],
): Array<{
  key: string;
  path: string;
  element: ReactNode;
}> {
  const byOverride = new Map<string, PluginManifest>();
  const addons: PluginManifest[] = [];

  for (const m of manifests) {
    if (m.tab.override) {
      byOverride.set(m.tab.override, m);
    } else {
      addons.push(m);
    }
  }

  const routes: Array<{
    key: string;
    path: string;
    element: ReactNode;
  }> = [];

  for (const [path, Component] of Object.entries(builtinRoutes)) {
    const om = byOverride.get(path);
    if (om) {
      routes.push({
        key: `override:${om.name}`,
        path,
        element: <PluginPage name={om.name} />,
      });
    } else {
      routes.push({ key: `builtin:${path}`, path, element: <Component /> });
    }
  }

  for (const m of addons) {
    if (m.tab.hidden) continue;
    if (m.tab.path === "/plugins") continue;
    if (builtinRoutes[m.tab.path]) continue;
    routes.push({
      key: `plugin:${m.name}`,
      path: m.tab.path,
      element: <PluginPage name={m.name} />,
    });
  }

  for (const m of manifests) {
    if (!m.tab.hidden) continue;
    if (m.tab.path === "/plugins") continue;
    if (builtinRoutes[m.tab.path] || m.tab.override) continue;
    routes.push({
      key: `plugin:hidden:${m.name}`,
      path: m.tab.path,
      element: <PluginPage name={m.name} />,
    });
  }

  return routes;
}

const SIDEBAR_COLLAPSED_KEY = "janus-sidebar-collapsed";

export default function App() {
  const { t } = useI18n();
  const { pathname } = useLocation();
  const { manifests, loading: pluginsLoading } = usePlugins();
  const { theme } = useTheme();
  const [mobileOpen, setMobileOpen] = useState(false);
  const closeMobile = useCallback(() => setMobileOpen(false), []);
  const [commandOpen, setCommandOpen] = useState(false);
  const openCommand = useCallback(() => {
    setMobileOpen(false);
    setCommandOpen(true);
  }, []);

  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true";
    } catch {
      return false;
    }
  });
  const isMobile = useBelowBreakpoint(1024);
  // One button drives the navigation at every width: it folds the column
  // to an icon rail on wide screens and opens it as a sheet on narrow ones.
  const toggleSidebar = useCallback(() => {
    if (isMobile) {
      setMobileOpen((prev) => !prev);
      return;
    }
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
      } catch { /* localStorage may be unavailable in private browsing */ }
      return next;
    });
  }, [isMobile]);

  const sidebarStatus = useSidebarStatus();
  const isDocsRoute = pathname === "/docs" || pathname === "/docs/";
  const normalizedPath = pathname.replace(/\/$/, "") || "/";
  const isChatRoute = normalizedPath === "/chat";
  const embeddedChat = isDashboardEmbeddedChatEnabled();

  // `dashboard.show_token_analytics` gates the Analytics nav item.  The
  // page itself remains reachable by URL (it renders an explanation when
  // the flag is off — see AnalyticsPage), but hiding the nav entry avoids
  // surfacing misleading token/cost numbers in the sidebar.  Default off.
  const [showTokenAnalytics, setShowTokenAnalytics] = useState(false);
  useEffect(() => {
    api
      .getConfig()
      .then((cfg) => {
        const dash = (cfg?.dashboard ?? {}) as {
          show_token_analytics?: unknown;
        };
        setShowTokenAnalytics(dash.show_token_analytics === true);
      })
      .catch(() => setShowTokenAnalytics(false));
  }, []);

  // A plugin can replace the built-in /chat page via `tab.override: "/chat"`
  // in its manifest.  When one does, `buildRoutes` already swaps the route
  // element for <PluginPage /> — but we also have to suppress the
  // persistent ChatPage host below, or the plugin's page and the built-in
  // terminal would paint on top of each other.  The override is niche
  // (nothing ships overriding /chat today) but it's an advertised
  // extension point, so preserve the pre-persistence contract: when a
  // plugin owns /chat, the built-in chat UI is entirely absent.
  //
  // Waiting on `pluginsLoading` is load-bearing: manifests arrive
  // asynchronously from /api/dashboard/plugins, so on initial render
  // `chatOverriddenByPlugin` is always false.  Without the loading
  // gate, the persistent host would mount, spawn a PTY, and THEN get
  // yanked out from under the user when the plugin's manifest resolves
  // — killing the session mid-paint.  Delaying host mount by the
  // plugin-load window (typically <50ms, worst case 2s safety timeout)
  // is the cheaper trade-off.
  const chatOverriddenByPlugin = useMemo(
    () => manifests.some((m) => m.tab.override === "/chat"),
    [manifests],
  );

  const builtinRoutes = useMemo(
    () => ({
      ...BUILTIN_ROUTES_CORE,
      ...(embeddedChat ? { "/chat": ChatRouteSink } : {}),
    }),
    [embeddedChat],
  );

  const builtinNav = useMemo(() => {
    const base = embeddedChat ? [CHAT_NAV_ITEM, ...BUILTIN_NAV] : BUILTIN_NAV;
    return showTokenAnalytics
      ? base
      : base.filter((n) => n.path !== "/analytics");
  }, [embeddedChat, showTokenAnalytics]);

  const navGroups = useMemo(
    () => buildNavGroups(builtinNav, manifests, t),
    [builtinNav, manifests, t],
  );
  const routes = useMemo(
    () => buildRoutes(builtinRoutes, manifests),
    [builtinRoutes, manifests],
  );
  // Page names for the top bar: the sidebar's own labels, so a page is
  // called the same thing in both places, plus plugin pages that replace
  // a built-in route.
  const pageTitles = useMemo(
    () => [
      ...manifests
        .filter((m) => !m.tab.hidden && m.tab.override)
        .map((m) => ({ path: m.tab.override as string, label: m.label })),
      ...navGroups.flatMap((g) =>
        g.items.map((item) => ({ path: item.path, label: navLabel(item, t) })),
      ),
    ],
    [manifests, navGroups, t],
  );

  const layoutVariant = theme.layoutVariant ?? "standard";
  const railCollapsed = collapsed && !isMobile;

  // Leaving the narrow layout closes the sheet, so it is not found open
  // again after the window is made narrow a second time.
  useEffect(() => {
    const wide = window.matchMedia("(min-width: 1024px)");
    const onChange = (e: MediaQueryListEvent) => {
      if (e.matches) setMobileOpen(false);
    };
    wide.addEventListener("change", onChange);
    return () => wide.removeEventListener("change", onChange);
  }, []);

  return (
    <TooltipProvider delayDuration={250} skipDelayDuration={300}>
      <div
        data-layout-variant={layoutVariant}
        className="flex h-dvh max-h-dvh min-h-0 overflow-hidden bg-background text-foreground"
      >
        <Backdrop />
        <PluginSlot name="backdrop" />

        <aside
          id="app-sidebar"
          className={cn(
            "relative z-2 hidden h-dvh shrink-0 border-r border-sidebar-border lg:block",
            "transition-[width] duration-200 ease-out",
            railCollapsed
              ? "w-[var(--sidebar-width-collapsed)]"
              : "w-[var(--sidebar-width)]",
          )}
          style={{
            background: "var(--component-sidebar-background, var(--sidebar))",
            borderImage: "var(--component-sidebar-border-image)",
            clipPath: "var(--component-sidebar-clip-path)",
          }}
        >
          <AppSidebar
            collapsed={railCollapsed}
            groups={navGroups}
            onOpenCommand={openCommand}
            status={sidebarStatus}
          />
        </aside>

        <Sheet open={mobileOpen && isMobile} onOpenChange={setMobileOpen}>
          <SheetContent>
            <SheetTitle className="sr-only">{t.app.navigation}</SheetTitle>
            <AppSidebar
              groups={navGroups}
              onNavigate={closeMobile}
              onOpenCommand={openCommand}
              status={sidebarStatus}
            />
          </SheetContent>
        </Sheet>

        <div className="relative z-2 flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          <PluginSlot name="header-banner" />

          <PageHeaderProvider
            onOpenCommand={openCommand}
            onToggleSidebar={toggleSidebar}
            sidebarExpanded={isMobile ? mobileOpen : !collapsed}
            titles={pageTitles}
          >
            <div
              className={cn(
                "relative flex min-h-0 min-w-0 flex-1 flex-col px-3 sm:px-6",
                isChatRoute ? "pb-0 pt-2 sm:pt-3" : "pt-4 sm:pt-6",
              )}
            >
              <PluginSlot name="pre-main" />
              <div
                className={cn(
                  "w-full min-w-0",
                  !isChatRoute &&
                    "pb-[calc(2rem+env(safe-area-inset-bottom,0px))] lg:pb-10",
                  (isDocsRoute || isChatRoute) &&
                    "flex min-h-0 flex-1 flex-col",
                )}
              >
                <Routes>
                  {routes.map(({ key, path, element }) => (
                    <Route key={key} path={path} element={element} />
                  ))}
                  <Route
                    path="*"
                    element={
                      <UnknownRouteFallback pluginsLoading={pluginsLoading} />
                    }
                  />
                </Routes>

                {embeddedChat &&
                  !chatOverriddenByPlugin &&
                  (pluginsLoading ? (
                    isChatRoute ? (
                      <div
                        className="flex min-h-0 min-w-0 flex-1 items-center justify-center"
                        aria-busy="true"
                        aria-live="polite"
                      >
                        <div className="flex items-center gap-2 text-sm text-muted-foreground">
                          <Spinner />
                          <span>Loading chat…</span>
                        </div>
                      </div>
                    ) : null
                  ) : (
                    <div
                      data-chat-active={isChatRoute ? "true" : "false"}
                      className={cn(
                        "min-h-0 min-w-0",
                        isChatRoute ? "flex flex-1 flex-col" : "hidden",
                      )}
                      aria-hidden={!isChatRoute}
                    >
                      <ChatPage isActive={isChatRoute} />
                    </div>
                  ))}
              </div>
              <PluginSlot name="post-main" />
            </div>
          </PageHeaderProvider>
        </div>

        <CommandMenu
          groups={navGroups}
          onOpenChange={setCommandOpen}
          open={commandOpen}
        />

        <PluginSlot name="overlay" />
      </div>
    </TooltipProvider>
  );
}
