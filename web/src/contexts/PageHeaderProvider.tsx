import { useMemo, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { PanelLeft, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { resolvePageTitle } from "@/lib/resolve-page-title";
import { cn } from "@/lib/utils";
import { PageHeaderContext } from "./page-header-context";

interface HeaderSlots {
  afterTitle: ReactNode;
  end: ReactNode;
  /** The path of the page these belong to. */
  owner: string;
  title: string | null;
}

function emptySlots(owner: string): HeaderSlots {
  return { afterTitle: null, end: null, owner, title: null };
}

interface PageHeaderProviderProps {
  children: ReactNode;
  onOpenCommand: () => void;
  onToggleSidebar: () => void;
  /** Whether the sidebar is currently showing its labels. */
  sidebarExpanded: boolean;
  /** Every navigable page with its translated name, plugin tabs included. */
  titles: { path: string; label: string }[];
}

/**
 * The top bar and the scrolling page area beneath it. The bar shows the
 * current page's name; a page can add controls beside the name
 * (`setAfterTitle`) and at the far end (`setEnd`) through `usePageHeader`.
 */
export function PageHeaderProvider({
  children,
  onOpenCommand,
  onToggleSidebar,
  sidebarExpanded,
  titles,
}: PageHeaderProviderProps) {
  const { pathname } = useLocation();
  const { t } = useI18n();
  const [slots, setSlots] = useState<HeaderSlots>(() => emptySlots(pathname));

  // What a page put in the bar belongs to the path it was set on. Reading
  // only the current path's slots means a page's controls disappear the
  // moment the route changes, with no effect needed to clear them.
  const live = slots.owner === pathname ? slots : emptySlots(pathname);
  const { afterTitle, end } = live;
  const defaultTitle = useMemo(
    () => resolvePageTitle(pathname, t, titles),
    [pathname, t, titles],
  );
  const displayTitle = live.title ?? defaultTitle;

  const isChatRoute = pathname === "/chat" || pathname === "/chat/";
  /** The Keys page puts a wide jump-nav beside the title; on small screens
   *  it goes on its own row so the title stays readable. */
  const isEnvRoute = pathname === "/env" || pathname.startsWith("/env/");

  // The setters are bound to the path they were handed out on, so the
  // order in which pages run their effects cannot matter: a page that is
  // leaving and clears its controls after the next page has already set
  // its own changes nothing, and neither does a page that stays mounted
  // while hidden (the embedded chat).
  const value = useMemo(() => {
    const write = (patch: Partial<Omit<HeaderSlots, "owner">>) =>
      setSlots((prev) => {
        if (prev.owner === pathname) return { ...prev, ...patch };
        const clearing = Object.values(patch).every((v) => v == null);
        return clearing ? prev : { ...emptySlots(pathname), ...patch };
      });
    return {
      setAfterTitle: (node: ReactNode) => write({ afterTitle: node }),
      setEnd: (node: ReactNode) => write({ end: node }),
      setTitle: (title: string | null) => write({ title }),
    };
  }, [pathname]);

  return (
    <PageHeaderContext.Provider value={value}>
      <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col overflow-hidden">
        <header
          role="banner"
          className={cn(
            "z-1 flex w-full shrink-0 gap-2 border-b border-border px-2 sm:px-4",
            // Small screens stack the title over the page's controls, so
            // the bar grows; from `sm` up it is one fixed-height row.
            "min-h-[var(--topbar-height)] items-start py-2.5 sm:h-[var(--topbar-height)] sm:items-center sm:py-0",
          )}
          style={{
            background: "var(--component-header-background, var(--canvas))",
            borderImage: "var(--component-header-border-image)",
            clipPath: "var(--component-header-clip-path)",
          }}
        >
          <Button
            ghost
            size="icon"
            onClick={onToggleSidebar}
            aria-label={
              sidebarExpanded ? t.common.collapse : t.app.openNavigation
            }
            aria-controls="app-sidebar"
            aria-expanded={sidebarExpanded}
            className="size-8 shrink-0 text-text-tertiary hover:text-foreground"
          >
            <PanelLeft />
          </Button>

          <div
            className={cn(
              "flex min-w-0 flex-1 gap-2 sm:gap-4",
              isChatRoute
                ? "flex-row items-center"
                : "flex-col justify-center sm:flex-row sm:items-center",
            )}
          >
            <div
              className={cn(
                "flex min-w-0 flex-1 gap-2 sm:gap-3",
                afterTitle && isEnvRoute
                  ? "flex-col items-start sm:flex-row sm:items-center"
                  : afterTitle
                    ? "flex-row flex-wrap items-center"
                    : "min-h-8 flex-row items-center",
              )}
            >
              <h1
                className={cn(
                  "min-w-0 font-display text-[1.3125rem] leading-none text-foreground",
                  afterTitle && isEnvRoute
                    ? "max-w-full sm:min-w-0 sm:shrink sm:truncate"
                    : afterTitle
                      ? "shrink truncate"
                      : "truncate",
                )}
              >
                {displayTitle}
              </h1>
              {afterTitle ? (
                <div
                  className={cn(
                    "min-w-0 scrollbar-none",
                    isEnvRoute
                      ? "w-full overflow-x-auto sm:flex-1"
                      : "shrink-0 overflow-visible",
                  )}
                >
                  {afterTitle}
                </div>
              ) : null}
            </div>

            {end ? (
              <div
                className={cn(
                  "flex min-w-0 sm:max-w-md sm:flex-1",
                  isChatRoute
                    ? "w-auto shrink-0 justify-end"
                    : "w-full justify-start sm:justify-end",
                )}
              >
                {end}
              </div>
            ) : null}
          </div>

          {/* On wide screens search lives in the sidebar. */}
          <Button
            ghost
            size="icon"
            onClick={onOpenCommand}
            aria-label={t.app.commandMenu.open}
            className="size-8 shrink-0 text-text-tertiary hover:text-foreground lg:hidden"
          >
            <Search />
          </Button>
        </header>

        <main
          className={cn(
            "flex min-h-0 w-full min-w-0 flex-1 flex-col",
            // Bottom inset for scrolled pages lives on the route outlet wrapper in
            // `App.tsx` so it pads scrollable content, not flex chrome.
            isChatRoute
              ? "overflow-hidden"
              : "overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable]",
          )}
        >
          {children}
        </main>
      </div>
    </PageHeaderContext.Provider>
  );
}
