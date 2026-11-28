import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

export interface ToastProps {
  toast: { message: string; type: "error" | "success" } | null;
}

export function Toast({ toast }: ToastProps) {
  if (!toast) return null;

  return createPortal(
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "fixed bottom-4 right-4 z-[120] flex max-w-sm animate-pop-in items-start gap-2.5 rounded-lg border border-border bg-popover px-3.5 py-2.5 text-sm text-foreground shadow-raised",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "mt-1.5 size-2 shrink-0 rounded-full",
          toast.type === "success" ? "bg-success" : "bg-destructive",
        )}
      />
      <span className="min-w-0 break-words">{toast.message}</span>
    </div>,
    document.body,
  );
}
