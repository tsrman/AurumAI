import * as React from "react";
import { cn } from "@/lib/utils";

export interface PillProps extends React.ComponentPropsWithRef<"button"> {
  active?: boolean;
}

export function Pill({
  type = "button",
  active,
  children,
  className,
  onClick,
  ref,
  ...props
}: PillProps) {
  return (
    <button
      ref={ref}
      type={type}
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-badge border px-3 py-1.5 text-sm font-semibold transition focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
        active
          ? "border-aurum-accent bg-aurum-accent-soft text-aurum-text"
          : "border-aurum-border bg-aurum-surface text-aurum-text-2 hover:bg-aurum-surface-3",
        className
      )}
      {...props}
    >
      {children}
    </button>
  );
}
