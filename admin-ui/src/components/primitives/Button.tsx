import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-btn px-4 py-2 font-semibold transition focus:outline-none focus:ring-2 focus:ring-aurum-accent/50 disabled:opacity-50",
  {
    variants: {
      variant: {
        primary:
          "bg-aurum-accent text-aurum-accent-ink shadow-card hover:brightness-105",
        secondary:
          "border border-aurum-border bg-aurum-surface text-aurum-text-2 hover:bg-aurum-surface-3",
        danger:
          "bg-aurum-err-fg text-white shadow-card hover:brightness-110",
      },
    },
    defaultVariants: { variant: "primary" },
  }
);

export interface ButtonProps
  extends React.ComponentPropsWithRef<"button">,
    VariantProps<typeof buttonVariants> {}

export function Button({
  type = "button",
  className,
  variant,
  ref,
  ...props
}: ButtonProps) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant }), className)}
      {...props}
    />
  );
}
