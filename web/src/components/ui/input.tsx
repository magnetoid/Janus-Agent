import * as React from "react";

import { cn } from "@/lib/utils";
import { controlClass } from "./control";

const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, ...props }, ref) => (
    <input
      type={type}
      ref={ref}
      className={cn(controlClass, "flex h-9 px-3 py-1", className)}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export { Input };
