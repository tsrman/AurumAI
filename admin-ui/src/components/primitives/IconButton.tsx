import * as React from "react";
import { cn } from "@/lib/utils";

type WithAriaLabel = { "aria-label": string; children?: React.ReactNode };
type WithChildren = { children: React.ReactNode; "aria-label"?: string };

export type IconButtonProps = React.ComponentPropsWithRef<"button"> & {
  bare?: boolean;
} & (WithAriaLabel | WithChildren);

export function IconButton({
  type = "button",
  bare,
  className,
  children,
  ref,
  ...props
}: IconButtonProps) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-btn transition focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
        bare
          ? "border-0 bg-transparent hover:bg-aurum-surface-3"
          : "border border-aurum-border bg-aurum-surface text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text",
        className
      )}
      {...props}
    >
      {children}
    </button>
  );
}
