import * as React from "react";
import { cn } from "@/lib/utils";
import type { BadgeColor } from "@/types/aurum";

const colorMap: Record<BadgeColor, string> = {
  amber: "bg-c-amber-bg text-c-amber-fg",
  green: "bg-c-green-bg text-c-green-fg",
  blue: "bg-c-blue-bg text-c-blue-fg",
  violet: "bg-c-violet-bg text-c-violet-fg",
  rose: "bg-c-rose-bg text-c-rose-fg",
};

export interface BadgeProps
  extends Omit<React.ComponentPropsWithRef<"span">, "color"> {
  color: BadgeColor;
}

export function Badge({
  color,
  children,
  className,
  ref,
  ...props
}: BadgeProps) {
  return (
    <span
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center rounded-badge px-2 py-0.5 text-xs font-bold",
        colorMap[color],
        className
      )}
      {...props}
    >
      {children}
    </span>
  );
}
