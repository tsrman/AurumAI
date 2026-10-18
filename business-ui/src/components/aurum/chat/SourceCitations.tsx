"use client";

import { useAppShell } from "@/hooks/useAppShell";
import type { Source } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface SourceCitationsProps {
  sources: Source[];
}

export function SourceCitations({ sources }: SourceCitationsProps) {
  const t = useTranslations("chat.sources");
  const { setDrawerSource } = useAppShell();

  if (sources.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-sm text-aurum-text-3">{t("label")}</span>
      {sources.map((source) => (
        <button
          key={source.index}
          type="button"
          onClick={() => setDrawerSource(source)}
          className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-aurum-accent-soft text-xs font-semibold text-aurum-accent-ink transition hover:bg-aurum-accent hover:text-aurum-accent-ink"
          aria-label={
            source.kind === "web"
              ? t("item", { index: source.index, title: source.title })
              : source.kind === "unsupported"
                ? t("unverified", { index: source.index })
                : t("item", { index: source.index, title: source.doc })
          }
        >
          {source.index}
        </button>
      ))}
    </div>
  );
}
