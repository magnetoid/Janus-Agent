import * as React from "react";

import { cn } from "@/lib/utils";

interface SegmentedOption<T extends string> {
  label: string;
  value: T;
}

interface SegmentedProps<T extends string> {
  className?: string;
  onChange: (value: T) => void;
  options: SegmentedOption<T>[];
  size?: "md" | "sm";
  value: T;
}

function Segmented<T extends string>({
  className,
  onChange,
  options,
  size = "md",
  value,
}: SegmentedProps<T>) {
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex items-center gap-0.5 rounded-lg border border-border bg-muted p-0.5",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "cursor-pointer rounded-md font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
              size === "sm" ? "px-2 py-0.5 text-xs" : "px-2.5 py-1 text-[0.8125rem]",
              active
                ? "bg-accent text-foreground"
                : "text-text-secondary hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

interface FilterGroupProps {
  children: React.ReactNode;
  className?: string;
  label: string;
}

function FilterGroup({ children, className, label }: FilterGroupProps) {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <span className="text-xs text-text-tertiary">
        {label}
      </span>
      {children}
    </div>
  );
}

export { Segmented, FilterGroup };
