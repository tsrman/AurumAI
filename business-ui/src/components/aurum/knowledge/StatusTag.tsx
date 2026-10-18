"use client";

import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslations } from "next-intl";

export interface StatusTagProps {
  status: "indexed" | "processing" | "failed";
  className?: string;
}

const PRESET = {
  indexed: {
    icon: CheckCircle2,
    label: "indexed",
    tone: "bg-aurum-ok-bg text-aurum-ok-fg",
    spin: false,
  },
  processing: {
    icon: Loader2,
    label: "processing",
    tone: "bg-aurum-accent-soft text-aurum-text-2",
    spin: true,
  },
  failed: {
    icon: AlertCircle,
    label: "failed",
    tone: "bg-aurum-err-bg text-aurum-err-fg",
    spin: false,
  },
} as const;

export function StatusTag({ status, className }: StatusTagProps) {
  const t = useTranslations("knowledge.status");
  const { icon: Icon, label, tone, spin } = PRESET[status];

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-chip px-2 py-0.5 text-xs font-medium",
        tone,
        className
      )}
    >
      <Icon className={cn("h-3.5 w-3.5", spin && "animate-spin")} aria-hidden="true" />
      {t(label)}
    </span>
  );
}
