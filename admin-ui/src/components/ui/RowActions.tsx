import * as React from "react";
import { cn } from "@/lib/utils";

/** Compact labelled action button for table rows. */
export function RowAction({
  icon,
  label,
  onClick,
  disabled,
  tone = "default",
  ariaLabel,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  tone?: "default" | "danger";
  ariaLabel?: string;
}) {
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      disabled={disabled}
      aria-label={ariaLabel}
      title={ariaLabel ?? label}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-btn px-2 py-1.5 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-aurum-accent/50 disabled:cursor-not-allowed disabled:opacity-40",
        tone === "danger"
          ? "text-aurum-text-3 hover:bg-aurum-err-bg hover:text-aurum-err-fg"
          : "text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text"
      )}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}

export function RowActions({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap items-center justify-end gap-1">{children}</div>;
}
