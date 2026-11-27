import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

// eslint-disable-next-line react-refresh/only-export-components -- shadcn cva export
export const badgeVariants = cva(
  "inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-xs font-medium leading-4",
  {
    variants: {
      variant: {
        // Tinted, not filled: a badge labels state, it is not a call to
        // action, so it must not compete with primary buttons.
        default: "border-transparent bg-primary/15 text-primary",
        secondary: "border-transparent bg-secondary text-text-secondary",
        destructive: "border-transparent bg-destructive/15 text-destructive",
        success: "border-transparent bg-success/15 text-success",
        warning: "border-transparent bg-warning/15 text-warning",
        outline: "border-border text-text-secondary",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
  /** Alias for `variant`, kept for plugins and older pages. */
  tone?: "default" | "destructive" | "outline" | "secondary" | "success" | "warning";
}

export function Badge({ className, variant, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant: tone ?? variant }), className)} {...props} />;
}
