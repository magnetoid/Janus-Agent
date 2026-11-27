import * as React from "react";

import { cn } from "@/lib/utils";

/** Placeholder block shown while content loads. Size it with classes. */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden
      className={cn("animate-pulse rounded-md bg-accent", className)}
      {...props}
    />
  );
}

export { Skeleton };
