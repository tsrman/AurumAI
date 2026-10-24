import * as React from "react";
import { cn } from "@/lib/utils";

export interface PageHeaderProps {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  /** Section tabs rendered under the title row. */
  tabs?: React.ReactNode;
  className?: string;
}

/** Title row shared by every admin page: heading, one-line purpose and the
 * page's primary actions, with optional section tabs underneath. */
export function PageHeader({ title, description, actions, tabs, className }: PageHeaderProps) {
  return (
    <header className={cn("border-b border-aurum-border-soft bg-aurum-surface px-6 pt-5", tabs ? "pb-0" : "pb-5", className)}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-aurum-text">{title}</h1>
          {description ? (
            <p className="mt-1 max-w-3xl text-sm leading-6 text-aurum-text-3">{description}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {tabs ? <div className="mt-4">{tabs}</div> : null}
    </header>
  );
}

/** Scrollable page body under a PageHeader. */
export function PageBody({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("flex-1 space-y-6 px-6 py-6", className)}>{children}</div>;
}
