/**
 * The shared look of every text-entry control (Input, Textarea, Select):
 * a recessed field whose border steps up on hover and turns brass on
 * focus. Heights and padding stay with each component.
 */
export const controlClass =
  "w-full rounded-md border border-input bg-background/50 text-sm text-foreground outline-none transition-[border-color,box-shadow] duration-150 placeholder:text-text-tertiary hover:border-border-strong focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/25";
