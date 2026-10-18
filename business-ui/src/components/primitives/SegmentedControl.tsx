import * as React from "react";
import { cn } from "@/lib/utils";

export type SegmentedControlProps<T extends string> = Omit<
  React.ComponentPropsWithRef<"div">,
  "value" | "onChange" | "options"
> & {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
};

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  className,
  ref,
  ...props
}: SegmentedControlProps<T>) {
  return (
    <div
      ref={ref}
      role="group"
      className={cn(
        "inline-flex rounded-btn border border-aurum-border bg-aurum-surface p-0.5",
        className
      )}
      {...props}
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          aria-pressed={value === opt.value}
          onClick={() => onChange(opt.value)}
          className={cn(
            "rounded-btn px-3 py-1.5 text-sm font-semibold transition focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
            value === opt.value
              ? "bg-aurum-accent-soft text-aurum-text"
              : "text-aurum-text-2 hover:text-aurum-text"
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
