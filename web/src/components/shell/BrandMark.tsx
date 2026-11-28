import { cn } from "@/lib/utils";

/**
 * The Janus mark: a coin split down the middle. The filled half looks
 * back (what the agent has done and remembers); the open half looks
 * ahead (what it is about to do). Inherits the text color.
 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className={cn("size-5 shrink-0", className)}
      fill="none"
    >
      <path d="M10.75 3a9 9 0 0 0 0 18Z" fill="currentColor" />
      <path
        d="M13.25 3.75a8.25 8.25 0 0 1 0 16.5Z"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
    </svg>
  );
}
