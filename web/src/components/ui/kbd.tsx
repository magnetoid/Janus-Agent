import * as React from "react";

import { cn } from "@/lib/utils";

/** A key cap, for showing keyboard shortcuts. */
function Kbd({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] border border-border bg-background/60 px-1 font-sans text-[0.6875rem] font-medium leading-none text-text-tertiary",
        className,
      )}
      {...props}
    />
  );
}

export { Kbd };
