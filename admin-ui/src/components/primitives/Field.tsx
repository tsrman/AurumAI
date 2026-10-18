import * as React from "react";
import { cn } from "@/lib/utils";

export interface FieldProps extends React.ComponentPropsWithRef<"input"> {
  label?: string;
}

export function Field({
  label,
  className,
  ref,
  ...props
}: FieldProps) {
  return (
    <label className={cn("block", className)}>
      {label && (
        <span className="mb-1.5 block text-sm font-medium text-aurum-text-2">
          {label}
        </span>
      )}
      <input
        ref={ref}
        className="w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text outline-none transition focus:border-aurum-accent focus:ring-2 focus:ring-aurum-accent/20"
        {...props}
      />
    </label>
  );
}
