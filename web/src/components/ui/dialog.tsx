import * as React from "react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";

export interface DialogProps {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: React.ReactNode;
}

/**
 * Modal dialog on Radix: focus is trapped inside and returns to the
 * trigger on close, the page behind is inert and does not scroll, and
 * Escape or a click on the scrim closes it.
 */
function Dialog({ open = false, onOpenChange, children }: DialogProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      {children}
    </DialogPrimitive.Root>
  );
}
Dialog.displayName = "Dialog";

export interface DialogContentProps extends React.HTMLAttributes<HTMLDivElement> {
  showCloseButton?: boolean;
}

const DialogContent = React.forwardRef<HTMLDivElement, DialogContentProps>(
  ({ className, children, showCloseButton = true, ...props }, ref) => (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-[110] animate-fade-in bg-black/60" />
      <DialogPrimitive.Content
        ref={ref}
        // Not every dialog has a description; callers that render
        // <DialogDescription> get it wired up by Radix regardless.
        aria-describedby={undefined}
        className={cn(
          "fixed left-1/2 top-1/2 z-[110] max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 animate-pop-in overflow-y-auto rounded-xl border border-border bg-popover p-6 text-popover-foreground shadow-raised outline-none",
          className,
        )}
        {...props}
      >
        {children}
        {showCloseButton ? (
          <DialogPrimitive.Close
            aria-label="Close"
            className="absolute right-3 top-3 inline-flex size-8 cursor-pointer items-center justify-center rounded-md text-text-tertiary outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X className="size-4" />
          </DialogPrimitive.Close>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  ),
);
DialogContent.displayName = "DialogContent";

const DialogHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex flex-col gap-1.5 pr-8", className)} {...props} />
  ),
);
DialogHeader.displayName = "DialogHeader";

const DialogFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn("mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end", className)}
      {...props}
    />
  ),
);
DialogFooter.displayName = "DialogFooter";

const DialogTitle = React.forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <DialogPrimitive.Title
      ref={ref}
      className={cn("text-base font-semibold leading-snug text-foreground", className)}
      {...props}
    />
  ),
);
DialogTitle.displayName = "DialogTitle";

const DialogDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn("text-sm text-muted-foreground", className)}
    {...props}
  />
));
DialogDescription.displayName = "DialogDescription";

const DialogClose = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement>
>(({ type = "button", ...props }, ref) => (
  <DialogPrimitive.Close ref={ref} type={type} {...props} />
));
DialogClose.displayName = "DialogClose";

const DialogTrigger = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement>
>(({ type = "button", ...props }, ref) => (
  <DialogPrimitive.Trigger ref={ref} type={type} {...props} />
));
DialogTrigger.displayName = "DialogTrigger";

/** Kept for import compatibility; `DialogContent` renders its own scrim. */
function DialogOverlay({ children }: { children?: React.ReactNode }) {
  return <>{children}</>;
}
DialogOverlay.displayName = "DialogOverlay";

/** Kept for import compatibility; `DialogContent` portals itself. */
function DialogPortal({ children }: { children?: React.ReactNode }) {
  return <>{children}</>;
}
DialogPortal.displayName = "DialogPortal";

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
};
