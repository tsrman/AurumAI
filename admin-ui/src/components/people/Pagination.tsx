"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { IconButton } from "@/components/primitives";

export interface PaginationProps {
  offset: number;
  pageSize: number;
  shown: number;
  total?: number;
  hasMore: boolean;
  onChange: (offset: number) => void;
}

export function Pagination({ offset, pageSize, shown, total, hasMore, onChange }: PaginationProps) {
  const t = useTranslations("people.pagination");
  if (offset === 0 && !hasMore) return null;
  return (
    <div className="flex items-center justify-end gap-3 text-sm text-aurum-text-3">
      <span>
        {total !== undefined
          ? t("rangeOf", { from: shown ? offset + 1 : 0, to: offset + shown, total })
          : t("range", { from: shown ? offset + 1 : 0, to: offset + shown })}
      </span>
      <IconButton aria-label={t("previous")} disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - pageSize))} className="disabled:opacity-40">
        <ChevronLeft className="h-4 w-4" aria-hidden="true" />
      </IconButton>
      <IconButton aria-label={t("next")} disabled={!hasMore} onClick={() => onChange(offset + pageSize)} className="disabled:opacity-40">
        <ChevronRight className="h-4 w-4" aria-hidden="true" />
      </IconButton>
    </div>
  );
}
