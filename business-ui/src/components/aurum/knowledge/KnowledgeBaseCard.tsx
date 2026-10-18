"use client";

import Link from "next/link";
import { FileText, HardDrive } from "lucide-react";
import { Badge } from "@/components/primitives/Badge";
import { StatusTag } from "./StatusTag";
import { cn } from "@/lib/utils";
import type { KnowledgeBase } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface KnowledgeBaseCardProps {
  base: KnowledgeBase;
}

export function KnowledgeBaseCard({ base }: KnowledgeBaseCardProps) {
  const t = useTranslations("knowledge.card");
  return (
    <Link
      href={`/knowledge/${base.id}`}
      className={cn(
        "group block rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-card",
        "transition hover:border-aurum-accent hover:shadow-card focus:outline-none focus:ring-2 focus:ring-aurum-accent/50"
      )}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <Badge color={base.badgeColor}>{base.badge}</Badge>
          <h3 className="truncate text-base font-semibold text-aurum-text">
            {base.name}
          </h3>
        </div>
        <StatusTag status={base.status} />
      </div>

      <p className="mt-3 line-clamp-2 text-sm text-aurum-text-2">
        {base.description}
      </p>

      <div className="mt-4 flex items-center gap-4 text-xs text-aurum-text-3">
        <span className="inline-flex items-center gap-1.5">
          <FileText className="h-3.5 w-3.5" aria-hidden="true" />
          {t("docCount", { count: base.docCount })}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <HardDrive className="h-3.5 w-3.5" aria-hidden="true" />
          {base.size}
        </span>
        <span className="ml-auto">{base.updated}</span>
      </div>
    </Link>
  );
}
