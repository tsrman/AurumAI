"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { listConversations } from "@/lib/chat-api";

export function useConversations(options?: { limit?: number; enabled?: boolean }) {
  const limit = options?.limit ?? 20;
  return useInfiniteQuery({
    queryKey: ["conversations", { limit }],
    queryFn: ({ pageParam }: { pageParam?: string }) =>
      listConversations(pageParam, limit),
    getNextPageParam: (lastPage) => {
      if (!lastPage.pagination?.hasMore) return undefined;
      return lastPage.pagination.cursor;
    },
    initialPageParam: undefined,
    enabled: options?.enabled ?? true,
  });
}
