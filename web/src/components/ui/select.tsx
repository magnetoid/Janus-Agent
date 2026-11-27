import * as React from "react";
import { ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";
import { controlClass } from "./control";

export interface SelectOptionProps {
  children: React.ReactNode;
  value: string;
}

/** Data carrier consumed by <Select>; renders nothing on its own. */
function SelectOption(_props: SelectOptionProps): null {
  return null;
}
SelectOption.displayName = "SelectOption";

export interface SelectProps {
  children?: React.ReactNode;
  className?: string;
  disabled?: boolean;
  id?: string;
  onValueChange?: (value: string) => void;
  placeholder?: string;
  style?: React.CSSProperties;
  value?: string;
}

const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ children, className, disabled, id, onValueChange, placeholder, style, value }, ref) => {
    const options = React.Children.toArray(children).filter(
      (child): child is React.ReactElement<SelectOptionProps> =>
        React.isValidElement(child) && child.type === SelectOption,
    );

    const showPlaceholder = placeholder != null && (value == null || value === "");

    return (
      <div className="relative w-full">
        <select
          ref={ref}
          id={id}
          disabled={disabled}
          style={style}
          value={value ?? ""}
          onChange={(event) => onValueChange?.(event.target.value)}
          className={cn(
            controlClass,
            "flex h-9 cursor-pointer appearance-none px-3 py-1 pr-8",
            className,
          )}
        >
          {showPlaceholder && (
            <option value="" disabled hidden>
              {placeholder}
            </option>
          )}
          {options.map((option, index) => (
            <option key={option.props.value || index} value={option.props.value}>
              {option.props.children}
            </option>
          ))}
        </select>
        <ChevronDown
          aria-hidden="true"
          className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-text-tertiary"
        />
      </div>
    );
  },
);
Select.displayName = "Select";

export { Select, SelectOption };
