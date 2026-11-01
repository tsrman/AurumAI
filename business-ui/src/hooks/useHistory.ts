"use client";

import { useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import { useAppShell } from "@/hooks/useAppShell";
import { useAuth } from "@/providers/AuthProvider";
import { useConversations } from "@/hooks/useConversations";
import { useUpdateConversation } from "@/hooks/useUpdateConversation";
import { useDeleteConversation } from "@/hooks/useDeleteConversation";
import { getConversation, toolCallsFromHistory } from "@/lib/chat-api";
import { loadSourcesForMessage } from "@/lib/web-sources-api";
import { filesFrom } from "@/lib/files-api";
import { previewsFrom } from "@/lib/previews-api";
import { imageIdsFrom } from "@/lib/images-api";
import { formatHistoryTime, historyBucket } from "@/lib/date";
import { useLocale, useTranslations } from "next-intl";
import type { Conversation, Message, Source } from "@/types/aurum";
import type { MessageResponse } from "@/types/chat";

export interface HistoryGroup {
  date: string;
  conversations: Conversation[];
}

export interface UseHistoryReturn {
  /** @deprecated Use `historyItems` instead. */
  groups: HistoryGroup[];
  historyItems: HistoryGroup[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  hasNextPage: boolean;
  fetchNextPage: () => void;
  isFetchingNextPage: boolean;
  refetch: () => void;
  openConversation: (id: string) => void;
  renameConversation: (id: string, newTitle: string) => void;
  deleteConversation: (id: string) => void;
  isRenaming: boolean;
  isDeleting: boolean;
}

function mapMessageToUIMessage(
  msg: MessageResponse,
  sources: Source[] = []
): Message {
  if (msg.role === "user") {
    return {
      id: msg.id,
      role: "user",
      content: msg.content ?? "",
      images: imageIdsFrom(msg.imageIds),
      files: filesFrom(msg.files),
    };
  }

  return {
    id: msg.id,
    role: "assistant",
    agentId: "general",
    thinking: "",
    tools: toolCallsFromHistory(msg.toolCalls, msg.toolResults),
    content: msg.content ?? "",
    widgets: [],
    sources,
    images: imageIdsFrom(msg.imageIds),
    files: filesFrom(msg.files),
    previews: previewsFrom(msg.previews),
    thinkingShown: "",
    contentShown: msg.content ?? "",
    phase: "done",
    thinkingOpen: false,
    thinkingSeconds: 0,
    startedAt: new Date(msg.createdAt).getTime(),
    faithfulnessScore: msg.faithfulnessScore,
    faithfulnessReason: msg.faithfulnessReason,
    failed: msg.failed === true,
  };
}

export function useHistory(options?: { limit?: number }): UseHistoryReturn {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const {
    setConversations,
    activeConversationId,
    setActiveConversationId,
  } = useAppShell();

  const {
    data,
    isLoading,
    isError,
    error,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
    refetch,
  } = useConversations({ limit: options?.limit ?? 20, enabled: isAuthenticated });

  const updateMutation = useUpdateConversation();
  const deleteMutation = useDeleteConversation();

  const t = useTranslations("history");
  const locale = useLocale();
  const bucketLabel = useCallback(
    (input?: string | null) => {
      const bucket = historyBucket(input);
      return bucket ? t(`buckets.${bucket}`) : "";
    },
    [t]
  );

  const flatConversations = useMemo(
    () => data?.pages.flatMap((page) => page.data) ?? [],
    [data]
  );

  const historyItems = useMemo<HistoryGroup[]>(() => {
    const map = new Map<string, Conversation[]>();

    for (const conv of flatConversations) {
      const dateKey = bucketLabel(conv.lastMessageAt ?? conv.createdAt);
      const list = map.get(dateKey) ?? [];
      list.push({
        id: conv.id,
        agentId: "general",
        title: conv.title || t("newConversation"),
        date: dateKey,
        time: formatHistoryTime(conv.lastMessageAt ?? conv.createdAt, locale),
        messages: [],
        model: conv.model,
        temperature: conv.temperature,
        lastMessageAt: conv.lastMessageAt ?? conv.createdAt,
        status: conv.status,
        messageCount: conv.messageCount,
        agent: conv.agent ?? null,
        agentDefinitionId: conv.agentDefinitionId ?? null,
      });
      map.set(dateKey, list);
    }

    return Array.from(map.entries()).map(([date, conversations]) => ({
      date,
      conversations,
    }));
  }, [flatConversations, bucketLabel, locale, t]);

  const openConversation = useCallback(
    async (id: string) => {
      const { conversation, messages } = await getConversation(id);

      const hydratedSources = await Promise.all(
        messages.map((message) => loadSourcesForMessage(id, message))
      );
      const uiMessages = messages.map((message, index) =>
        mapMessageToUIMessage(message, hydratedSources[index])
      );
      const uiConversation: Conversation = {
        id: conversation.id,
        agentId: "general",
        title: conversation.title || t("newConversation"),
        date: bucketLabel(conversation.lastMessageAt ?? conversation.createdAt),
        time: formatHistoryTime(conversation.lastMessageAt ?? conversation.createdAt, locale),
        messages: uiMessages,
        model: conversation.model,
        temperature: conversation.temperature,
        lastMessageAt: conversation.lastMessageAt ?? conversation.createdAt,
        status: conversation.status,
        messageCount: messages.length,
        agent: conversation.agent ?? null,
        agentDefinitionId: conversation.agentDefinitionId ?? null,
      };

      setConversations((prev) => {
        const filtered = prev.filter((c) => c.id !== id);
        return [uiConversation, ...filtered];
      });
      setActiveConversationId(id);
      router.push("/chat");
    },
    [router, setActiveConversationId, setConversations, t, bucketLabel, locale]
  );

  const renameConversation = useCallback(
    (id: string, newTitle: string) => {
      updateMutation.mutate({ id, title: newTitle });
    },
    [updateMutation]
  );

  const deleteConversation = useCallback(
    (id: string) => {
      deleteMutation.mutate(id, {
        onSuccess: () => {
          if (activeConversationId === id) {
            setActiveConversationId(null);
          }
        },
      });
    },
    [activeConversationId, deleteMutation, setActiveConversationId]
  );

  return {
    groups: historyItems,
    historyItems,
    // While auth hasn't resolved yet, the
    // query is disabled (never fetches), so react-query's own isLoading
    // would read false here — treat "not authenticated yet" as loading too,
    // so the sidebar shows a spinner instead of a misleading empty state.
    isLoading: !isAuthenticated || isLoading,
    isError,
    error: error ?? null,
    hasNextPage: !!hasNextPage,
    fetchNextPage: () => {
      fetchNextPage();
    },
    isFetchingNextPage,
    refetch,
    openConversation,
    renameConversation,
    deleteConversation,
    isRenaming: updateMutation.isPending,
    isDeleting: deleteMutation.isPending,
  };
}
