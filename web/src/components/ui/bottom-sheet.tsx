import * as React from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";

export interface BottomSheetProps {
  backdropDismissLabel?: string;
  children: React.ReactNode;
  onClose: () => void;
  open: boolean;
  title: string;
}

function BottomSheet({ backdropDismissLabel, children, onClose, open, title }: BottomSheetProps) {
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <>
      <div
        className="fixed inset-0 z-[110] animate-fade-in bg-black/60"
        onClick={onClose}
        aria-hidden="true"
        title={backdropDismissLabel}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cn(
          "fixed inset-x-0 bottom-0 z-[110] max-h-[85dvh] animate-slide-in-up overflow-y-auto rounded-t-2xl border-t border-border bg-popover p-4 pb-[calc(1rem+env(safe-area-inset-bottom,0px))] shadow-raised",
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&_svg]:size-4 [&_svg]:shrink-0"
          >
            <X />
            <span className="sr-only">Close</span>
          </button>
        </div>
        {children}
      </div>
    </>,
    document.body,
  );
}

export { BottomSheet };
