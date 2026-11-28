import * as React from "react";

import { cn } from "@/lib/utils";

export interface TypographyProps extends React.HTMLAttributes<HTMLElement> {
  as?: React.ElementType;
  /** Font flags from the previous design system. Only the mono ones still
   *  do anything; the rest are accepted so older call sites compile. */
  mondwest?: boolean;
  expanded?: boolean;
  compressed?: boolean;
  courier?: boolean;
  mono?: boolean;
  sans?: boolean;
}

/**
 * Polymorphic text element. Defaults to a `span`; pages supply their own
 * className. Accepts the legacy DS font flags so existing call sites keep compiling.
 */
export function Typography({
  as,
  className,
  mondwest: _mondwest,
  expanded: _expanded,
  compressed: _compressed,
  courier,
  mono,
  sans,
  children,
  ...rest
}: TypographyProps) {
  const Tag = (as ?? "span") as React.ElementType;
  const font = mono || courier
      ? "font-mono"
      : sans
        ? "font-sans"
        : undefined;
  return (
    <Tag className={cn(font, className)} {...rest}>
      {children}
    </Tag>
  );
}
