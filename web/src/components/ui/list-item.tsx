import * as React from "react";

import { cn } from "@/lib/utils";

export interface ListItemProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
}

const ListItem = React.forwardRef<HTMLButtonElement, ListItemProps>(
  ({ className, active, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        "flex w-full cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-left text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
        active ? "bg-accent text-accent-foreground" : "text-text-secondary hover:bg-accent/60 hover:text-foreground",
        className,
      )}
      {...props}
    />
  ),
);
ListItem.displayName = "ListItem";

export { ListItem };
