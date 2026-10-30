"use client";

import { useCallback } from "react";
import { ChatHeader } from "@/components/aurum/chat/ChatHeader";
import { Composer } from "@/components/aurum/chat/Composer";
import { MessageList } from "@/components/aurum/chat/MessageList";
import { AgentCard } from "@/components/aurum/chat/AgentCard";
import { SuggestionGrid } from "@/components/aurum/chat/SuggestionGrid";
import { SourceDrawer } from "@/components/aurum/chat/SourceDrawer";
import { useChat } from "@/hooks/useChat";
import { useAuth } from "@/providers/AuthProvider";
import { useAppShell } from "@/hooks/useAppShell";
import { useTranslations } from "next-intl";
import type { ChatAgentId } from "@/lib/chat-api";
import type { AttachedFile } from "@/lib/files-api";

export default function ChatPage() {
  const t = useTranslations("chat");
  const {
    messages,
    send,
    regenerate,
    isStreaming,
    cancel,
    error,
    activeConversationId,
    questions,
    hasActiveTurn,
    answerQuestion,
    ensureConversationId,
  } = useChat();
  const { isLoading: authLoading } = useAuth();
  const { activeModelId } = useAppShell();
  const admissionPending = authLoading || !activeModelId;
  const hasMessages = messages.length > 0;

  // useChat's isRealMode() reads false until
  // window.keycloak.token is set — a message sent before Keycloak finishes
  // initializing silently goes through the mock chat path instead of a real
  // request, and that mock conversation 422s the moment real mode kicks in
  // right after. The model catalog/settings can still be loading after
  // auth completes. Guard typing, attachments and suggestions until a
  // selected model exists so no empty-model conversation is created.
  const guardedSend = useCallback(
    (
      text: string,
      workflowId?: string,
      imageIds?: string[],
      agent?: ChatAgentId,
      files?: AttachedFile[]
    ) => {
      if (admissionPending) return;
      send(text, workflowId, imageIds, agent, files);
    },
    [admissionPending, send]
  );

  return (
    <div className="flex h-full flex-1 flex-col">
      <ChatHeader />

      {hasMessages ? (
        <MessageList
          messages={messages}
          questions={questions}
          onRegenerate={regenerate}
          onAnswer={answerQuestion}
        />
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-6 px-6 py-12">
          <AgentCard />
          <div className="w-full max-w-2xl">
            <SuggestionGrid onSuggestion={guardedSend} />
          </div>
        </div>
      )}

      {error && (
        <div className="mx-6 mb-2 rounded-md border border-aurum-err-fg/30 bg-aurum-err-bg px-4 py-2 text-sm text-aurum-err-fg">
          {error}
        </div>
      )}

      {!authLoading && !activeModelId && (
        <p role="status" className="mx-6 mb-2 text-sm text-aurum-muted">
          {t("selectModel")}
        </p>
      )}

      <Composer
        onSend={guardedSend}
        isStreaming={isStreaming}
        onStop={cancel}
        disabled={admissionPending}
        blockedByActiveTurn={hasActiveTurn}
        conversationId={activeConversationId}
        ensureConversationId={ensureConversationId}
      />
      <SourceDrawer />
    </div>
  );
}
