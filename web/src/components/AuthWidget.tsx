/**
 * AuthWidget — sidebar "Logged in as …" affordance for the dashboard
 * OAuth gate (Phase 7 of .janus/plans/2026-05-21-dashboard-oauth-auth.md).
 *
 * Renders nothing in loopback / --insecure mode. In gated mode, fetches
 * /api/auth/me on mount and surfaces:
 *
 *   - the display_name / email when the provider supplies them, else the
 *     user_id (truncated to 14 chars + ellipsis) — some identity providers
 *     don't emit email/display_name claims and the API responds with empty
 *     strings for those fields
 *   - the provider's display_name (looked up from /api/auth/providers,
 *     defaults to the bare provider key)
 *   - a logout button that POSTs /auth/logout and full-page-navigates to
 *     /login (the dashboard becomes inaccessible again)
 *
 * Failure modes:
 *   - 401 from /api/auth/me means we're not gated (or the gate is on but
 *     we have no cookie — in that case the gate's middleware would have
 *     redirected us before App.tsx renders, so we won't see this). The
 *     widget renders nothing.
 *   - Network error: shows a minimal "auth status unavailable" message
 *     so the user knows the widget tried.
 */

import { useEffect, useState } from "react";
import { api, type AuthMeResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LogOut } from "lucide-react";

interface AuthWidgetProps {
  className?: string;
}

/** Truncate ``user_id`` to fit a small UI without revealing the full
 *  opaque identifier. 14 chars is enough to disambiguate users in a
 *  small org and short enough to fit a single sidebar row. */
function truncateUserId(id: string): string {
  if (id.length <= 14) return id;
  return `${id.slice(0, 14)}…`;
}

export function AuthWidget({ className }: AuthWidgetProps) {
  const [me, setMe] = useState<AuthMeResponse | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getAuthMe()
      .then((data) => {
        if (cancelled) return;
        setMe(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // 401 from /api/auth/me means the gate isn't engaged in this
        // process (loopback mode) — render nothing. fetchJSON throws an
        // Error with the status code as a prefix; the global 401
        // handler only redirects on the structured envelope, so a plain
        // 401 from /api/auth/me with no envelope bubbles up here.
        const msg = err instanceof Error ? err.message : String(err);
        if (msg.startsWith("401:") || msg.startsWith("403:")) {
          setHidden(true);
          return;
        }
        setError("auth status unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (hidden) return null;

  if (error) {
    return (
      <div className={cn("px-2.5 py-1.5 text-xs text-text-tertiary", className)}>
        {error}
      </div>
    );
  }

  if (!me) {
    // Loading. Reserve the row height so the sidebar doesn't jump when
    // the data arrives.
    return <div className={cn("h-11", className)} aria-busy="true" />;
  }

  const handleLogout = () => {
    void api.logout();
  };

  // Prefer display_name → email → truncated user_id. Not every identity
  // provider populates the name/email claims.
  const label = me.display_name || me.email || truncateUserId(me.user_id);

  return (
    <div
      className={cn(
        "flex shrink-0 items-center justify-between gap-2 px-2.5 py-1.5",
        className,
      )}
      role="status"
      aria-label={`Logged in as ${label}`}
    >
      <div className="flex min-w-0 flex-col">
        <span className="truncate text-xs text-foreground" title={me.user_id}>
          {label}
        </span>
        <span className="truncate text-xs text-text-tertiary">
          via {me.provider}
        </span>
      </div>
      <button
        type="button"
        onClick={handleLogout}
        className="inline-flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-md text-text-tertiary outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        aria-label="Log out"
        title="Log out"
      >
        <LogOut className="size-4" />
      </button>
    </div>
  );
}
