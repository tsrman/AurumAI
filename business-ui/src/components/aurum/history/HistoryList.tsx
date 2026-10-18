"use client";

import { useEffect, useMemo, useRef } from "react";
import { Loader2 } from "lucide-react";
import { useAppShell } from "@/hooks/useAppShell";
import { useHistory } from "@/hooks/useHistory";
import { HistoryGroup } from "./HistoryGroup";
import { HistoryItem } from "./HistoryItem";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

export function HistoryList() {
  const t = useTranslations("history.list");
  const errorMessage = useErrorMessage();
  const {
    historyItems,
    isLoading,
    isError,
    error,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
    openConversation,
    refetch,
  } = useHistory();

  const { agents, myAgents } = useAppShell();
  const allAgents = useMemo(
    () => [...agents, ...myAgents],
    [agents, myAgents]
  );

  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!hasNextPage || isFetchingNextPage) return;

    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    const observer = new IntersectionObserver(
      (entries) => {
        const [entry] = entries;
        if (entry?.isIntersecting) {
          fetchNextPage();
        }
      },
      { rootMargin: "200px" }
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage]);

  if (isLoading && historyItems.length === 0) {
    return (
      <div
        className="flex items-center justify-center py-12 text-aurum-text-3"
        role="status"
        aria-live="polite"
      >
        <Loader2 className="mr-2 h-5 w-5 animate-spin" aria-hidden="true" />
        {t("loading")}
      </div>
    );
  }

  if (isError && historyItems.length === 0) {
    return (
      <div
        className="rounded-card border border-aurum-border bg-aurum-surface p-6 text-center shadow-card"
        role="alert"
      >
        <p className="text-aurum-err-fg">{t("loadFailed")}</p>
        {error?.message && (
          <p className="mt-1 text-sm text-aurum-err-fg/80">{errorMessage(error)}</p>
        )}
        <button
          type="button"
          onClick={() => refetch()}
          className="mt-4 rounded-md bg-aurum-accent px-4 py-2 text-sm font-medium text-white hover:bg-aurum-accent/90 focus:outline-none focus:ring-2 focus:ring-aurum-accent/50"
        >
          {t("retry")}
        </button>
      </div>
    );
  }

  if (historyItems.length === 0) {
    return (
      <div className="py-12 text-center text-aurum-text-3">
        {t("empty")}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      {historyItems.map((group) => (
        <HistoryGroup key={group.date} date={group.date}>
          {group.conversations.map((conversation) => (
            <HistoryItem
              key={conversation.id}
              conversation={conversation}
              agent={allAgents.find((a) => a.id === conversation.agentId)}
              onClick={() => openConversation(conversation.id)}
            />
          ))}
        </HistoryGroup>
      ))}

      {isFetchingNextPage && (
        <div
          className="flex justify-center py-4 text-aurum-text-3"
          role="status"
          aria-live="polite"
        >
          <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
        </div>
      )}

      {hasNextPage && (
        <div
          ref={sentinelRef}
          data-testid="history-sentinel"
          className="h-8"
          aria-hidden="true"
        />
      )}
    </div>
  );
}
