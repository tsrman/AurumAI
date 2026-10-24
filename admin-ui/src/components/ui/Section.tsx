import * as React from "react";
import { cn } from "@/lib/utils";

export interface SectionProps {
  title?: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

/** Titled card grouping related settings or content. */
export function Section({ title, description, actions, children, className }: SectionProps) {
  return (
    <section className={cn("rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-card", className)}>
      {title || actions ? (
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            {title ? <h2 className="text-base font-semibold text-aurum-text">{title}</h2> : null}
            {description ? <p className="mt-1 text-sm leading-6 text-aurum-text-3">{description}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 gap-2">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export interface SettingRowProps {
  label: string;
  description?: React.ReactNode;
  htmlFor?: string;
  children: React.ReactNode;
}

/** One setting: label and help text on the left, its control on the right. */
export function SettingRow({ label, description, htmlFor, children }: SettingRowProps) {
  return (
    <div className="flex flex-col gap-3 border-t border-aurum-border-soft py-4 first:border-t-0 first:pt-0 last:pb-0 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0 sm:max-w-md">
        {htmlFor ? (
          <label htmlFor={htmlFor} className="text-sm font-medium text-aurum-text">{label}</label>
        ) : (
          <p className="text-sm font-medium text-aurum-text">{label}</p>
        )}
        {description ? <p className="mt-1 text-xs leading-5 text-aurum-text-3">{description}</p> : null}
      </div>
      <div className="min-w-0 sm:w-80 sm:shrink-0">{children}</div>
    </div>
  );
}

export const controlClass =
  "w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text outline-none transition focus:border-aurum-accent focus:ring-2 focus:ring-aurum-accent/20 disabled:opacity-60";
