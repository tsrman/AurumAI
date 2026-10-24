import * as React from "react";
import { cn } from "@/lib/utils";

export interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center rounded-card border border-dashed border-aurum-border bg-aurum-surface px-6 py-10 text-center", className)}>
      {icon ? <div className="mb-3 text-aurum-text-3">{icon}</div> : null}
      <p className="text-base font-semibold text-aurum-text">{title}</p>
      {description ? <p className="mt-2 max-w-lg text-sm leading-6 text-aurum-text-3">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}
