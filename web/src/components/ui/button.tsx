import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

// eslint-disable-next-line react-refresh/only-export-components -- shadcn cva export
export const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer select-none items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium outline-none transition-[background-color,border-color,color] duration-150 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 aria-busy:cursor-progress [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground hover:bg-primary/90 active:bg-primary/80",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-accent active:bg-accent/80",
        destructive:
          "bg-destructive text-destructive-foreground hover:bg-destructive/90 active:bg-destructive/80",
        outline:
          "border border-input bg-transparent hover:border-border-strong hover:bg-accent hover:text-accent-foreground",
        ghost: "hover:bg-accent hover:text-accent-foreground",
        link: "h-auto px-0 text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-3.5",
        sm: "h-8 px-3 text-[0.8125rem]",
        xs: "h-7 gap-1.5 px-2 text-xs",
        lg: "h-10 px-5",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

type Variant = NonNullable<VariantProps<typeof buttonVariants>["variant"]>;

export interface ButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "prefix">,
    VariantProps<typeof buttonVariants> {
  /** Boolean shortcuts for `variant`, kept for plugins and older pages. */
  ghost?: boolean;
  destructive?: boolean;
  outlined?: boolean;
  invert?: boolean;
  prefix?: React.ReactNode;
  suffix?: React.ReactNode;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { className, variant, size, ghost, destructive, outlined, invert, prefix, suffix, children, ...props },
    ref,
  ) => {
    // `destructive` alone is the filled red button. With `ghost` or
    // `outlined` it keeps that quiet shape and only takes the red tone,
    // which is what a delete action in a row of items wants.
    const quiet: Variant | null = ghost ? "ghost" : outlined ? "outline" : null;
    const resolved: Variant = quiet
      ? quiet
      : destructive
        ? "destructive"
        : invert
          ? "secondary"
          : (variant ?? "default");
    const tone =
      destructive && quiet === "ghost"
        ? "text-destructive hover:bg-destructive/15 hover:text-destructive"
        : destructive && quiet === "outline"
          ? "border-destructive/40 text-destructive hover:border-destructive/70 hover:bg-destructive/15 hover:text-destructive"
          : undefined;
    return (
      <button ref={ref} className={cn(buttonVariants({ variant: resolved, size }), tone, className)} {...props}>
        {prefix}
        {children}
        {suffix}
      </button>
    );
  },
);
Button.displayName = "Button";
