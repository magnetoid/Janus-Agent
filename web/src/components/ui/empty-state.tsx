import * as React from "react";

import { cn } from "@/lib/utils";

export interface EmptyStateProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  /** The one next step, usually a button. */
  action?: React.ReactNode;
  /** What goes here and how to get some. */
  description?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  title: React.ReactNode;
}

/**
 * What a list, table or panel shows when it has nothing to show. Says
 * what belongs here and offers the action that fills it.
 */
function EmptyState({ action, className, description, icon: Icon, title, ...props }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border px-6 py-12 text-center",
        className,
      )}
      {...props}
    >
      {Icon ? (
        <span className="flex size-10 items-center justify-center rounded-lg bg-muted text-text-tertiary">
          <Icon className="size-5" />
        </span>
      ) : null}
      <div className="flex max-w-sm flex-col gap-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {description ? (
          <p className="text-sm text-text-secondary">{description}</p>
        ) : null}
      </div>
      {action}
    </div>
  );
}

export { EmptyState };
