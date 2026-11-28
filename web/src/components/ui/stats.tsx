import * as React from "react";

import { cn } from "@/lib/utils";

type Slot = string | { key: string; node: React.ReactNode };

export interface StatsItem {
  label: Slot;
  value: Slot;
}

export interface StatsProps extends React.ComponentProps<"div"> {
  items: StatsItem[];
  flip?: boolean;
}

function renderSlot(slot: Slot): React.ReactNode {
  if (typeof slot === "string") return slot;
  return <React.Fragment key={slot.key}>{slot.node}</React.Fragment>;
}

function Stats({ items, flip, className, ...props }: StatsProps) {
  return (
    <div className={cn("grid grid-cols-2 gap-4 sm:grid-cols-4", className)} {...props}>
      {items.map((item, i) => {
        const value = (
          <div className="text-2xl font-semibold tabular-nums leading-tight text-foreground">
            {renderSlot(item.value)}
          </div>
        );
        const label = (
          <div className="text-xs text-text-tertiary">
            {renderSlot(item.label)}
          </div>
        );
        return (
          <div key={i} className="flex flex-col gap-1">
            {flip ? (
              <>
                {label}
                {value}
              </>
            ) : (
              <>
                {value}
                {label}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

export { Stats };
