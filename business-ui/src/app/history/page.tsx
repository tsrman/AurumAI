"use client";

import { HistoryList } from "@/components/aurum/history/HistoryList";
import { useTranslations } from "next-intl";

export default function HistoryPage() {
  const t = useTranslations("history.page");
  return (
    <div className="flex h-full flex-1 flex-col">
      <header className="border-b border-aurum-border-soft bg-aurum-surface px-6 py-4">
        <h1 className="text-base font-semibold text-aurum-text">{t("title")}</h1>
        <p className="text-sm text-aurum-text-3">{t("subtitle")}</p>
      </header>

      <main className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-3xl">
          <HistoryList />
        </div>
      </main>
    </div>
  );
}
