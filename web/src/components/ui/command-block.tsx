import * as React from "react";
import { Check, Copy } from "lucide-react";

import { cn } from "@/lib/utils";

export interface CopyButtonProps {
  children?: React.ReactNode;
  className?: string;
  copiedLabel?: string;
  label?: string;
  resetDelayMs?: number;
  text: string;
}

function CopyButton({
  children,
  className,
  copiedLabel,
  label,
  resetDelayMs,
  text,
}: CopyButtonProps) {
  const [copied, setCopied] = React.useState(false);
  const timeoutRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = React.useRef(true);

  React.useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const handleCopy = React.useCallback(() => {
    void navigator.clipboard.writeText(text).then(() => {
      if (!mountedRef.current) return; // guard against post-unmount setState
      setCopied(true);
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      timeoutRef.current = setTimeout(() => setCopied(false), resetDelayMs ?? 1500);
    });
  }, [text, resetDelayMs]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={cn(
        "inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-input px-2 py-1 text-xs text-text-secondary outline-none transition-colors hover:border-border-strong hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    >
      {copied ? (
        <Check className="size-3.5 shrink-0" />
      ) : (
        <Copy className="size-3.5 shrink-0" />
      )}
      <span>{copied ? copiedLabel ?? "Copied" : label ?? children ?? "Copy"}</span>
    </button>
  );
}

export interface CommandBlockProps {
  className?: string;
  code: string;
  label: string;
}

function CommandBlock({ className, code, label }: CommandBlockProps) {
  return (
    <div className={cn("overflow-hidden rounded-lg border border-border bg-background/50", className)}>
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <span className="text-xs text-text-tertiary">{label}</span>
        <CopyButton text={code} />
      </div>
      <pre className="overflow-x-auto p-3 text-xs leading-relaxed">
        <code>{code}</code>
      </pre>
    </div>
  );
}

export { CommandBlock, CopyButton };
