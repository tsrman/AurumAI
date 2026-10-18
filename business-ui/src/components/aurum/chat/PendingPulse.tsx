"use client";

import { useTranslations } from "next-intl";

export function PendingPulse() {
  const t = useTranslations("chat");
  return (
    <div
      data-testid="pending-pulse"
      className="flex w-fit items-center gap-3 rounded-card border border-aurum-border bg-aurum-surface-2 px-4 py-2.5"
    >
      <span className="relative flex h-2.5 w-2.5 flex-none">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-aurum-accent opacity-75" />
        <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-aurum-accent" />
      </span>
      <span className="aurum-shimmer-text text-sm font-medium">
        {t("pending")}
      </span>
      <span className="flex items-center gap-1" aria-hidden="true">
        <span
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-aurum-accent"
          style={{ animationDelay: "0ms" }}
        />
        <span
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-aurum-accent"
          style={{ animationDelay: "150ms" }}
        />
        <span
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-aurum-accent"
          style={{ animationDelay: "300ms" }}
        />
      </span>
    </div>
  );
}
