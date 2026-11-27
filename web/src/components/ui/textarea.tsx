import * as React from "react";

import { cn } from "@/lib/utils";
import { controlClass } from "./control";

const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(controlClass, "flex min-h-[60px] px-3 py-2", className)}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";

export { Textarea };
