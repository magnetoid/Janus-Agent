import * as React from "react";

import { cn } from "@/lib/utils";

interface TabsProps {
  children: (active: string, setActive: (v: string) => void) => React.ReactNode;
  className?: string;
  defaultValue: string;
}

function Tabs({ children, className, defaultValue }: TabsProps) {
  const [active, setActive] = React.useState(defaultValue);
  return <div className={cn(className)}>{children(active, setActive)}</div>;
}

const TabsList = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-lg border border-border bg-muted p-0.5",
        className,
      )}
      {...props}
    />
  ),
);
TabsList.displayName = "TabsList";

interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  active: boolean;
  value: string;
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ active, value: _value, className, type, ...props }, ref) => (
    <button
      ref={ref}
      type={type ?? "button"}
      role="tab"
      aria-selected={active}
      className={cn(
        "inline-flex cursor-pointer items-center rounded-md px-3 py-1 text-sm font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
        active
          ? "bg-accent text-foreground"
          : "text-text-secondary hover:text-foreground",
        className,
      )}
      {...props}
    />
  ),
);
TabsTrigger.displayName = "TabsTrigger";

export { Tabs, TabsList, TabsTrigger };
