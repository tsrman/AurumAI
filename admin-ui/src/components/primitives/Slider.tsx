import * as React from "react";
import { cn } from "@/lib/utils";

export type SliderProps = Omit<
  React.ComponentPropsWithRef<"input">,
  "value" | "onChange" | "min" | "max" | "step" | "type"
> & {
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
};

export function Slider({
  value,
  min,
  max,
  step,
  onChange,
  className,
  ref,
  ...props
}: SliderProps) {
  return (
    <input
      ref={ref}
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      onChange={(e) => onChange(parseFloat(e.target.value))}
      className={cn(
        "h-1.5 w-full cursor-pointer appearance-none rounded-full bg-aurum-border accent-aurum-accent focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
        className
      )}
      {...props}
    />
  );
}
