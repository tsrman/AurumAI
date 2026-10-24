import * as React from "react";
import { cn } from "@/lib/utils";

type WithAriaLabel = { "aria-label": string; "aria-labelledby"?: string };
type WithAriaLabelledBy = { "aria-labelledby": string; "aria-label"?: string };

export type ToggleProps = Omit<
  React.ComponentPropsWithRef<"input">,
  "type" | "onChange" | "aria-label" | "aria-labelledby"
> & {
  checked: boolean;
  onChange: (v: boolean) => void;
} & (WithAriaLabel | WithAriaLabelledBy);

export function Toggle({
  checked,
  onChange,
  className,
  ref,
  ...props
}: ToggleProps) {
  return (
    <label className={cn("relative inline-flex cursor-pointer items-center", className)}>
      <input
        ref={ref}
        type="checkbox"
        // Transparent but laid over the track rather than sr-only, so the
        // real checkbox is what pointers (and browser automation) hit.
        className="peer absolute inset-0 z-10 m-0 h-full w-full cursor-pointer opacity-0"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        {...props}
      />
      <div className="h-5 w-9 rounded-full bg-aurum-border transition peer-checked:bg-aurum-accent peer-focus:ring-2 peer-focus:ring-aurum-accent/50" />
      <div className="absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition peer-checked:translate-x-4" />
    </label>
  );
}
