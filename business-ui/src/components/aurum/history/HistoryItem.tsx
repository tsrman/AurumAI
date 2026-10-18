"use client";

import { useRef, useState } from "react";
import { MessageSquare } from "lucide-react";
import { Badge } from "@/components/primitives/Badge";
import { cn } from "@/lib/utils";
import { formatShortDate } from "@/lib/date";
import { useHistory } from "@/hooks/useHistory";
import { ConversationActionsMenu } from "./ConversationActionsMenu";
import type { Agent, Conversation, Message } from "@/types/aurum";
import { useLocale, useTranslations } from "next-intl";

export interface HistoryItemProps {
  conversation: Conversation;
  agent: Agent | undefined;
  onClick: () => void;
}

function getSnippet(messages: Message[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role === "user") return msg.content;
  }
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role === "assistant") return msg.contentShown || msg.content;
  }
  return "";
}

export function HistoryItem({
  conversation,
  agent,
  onClick,
}: HistoryItemProps) {
  const locale = useLocale();
  const t = useTranslations("history.item");
  const { renameConversation, deleteConversation, isRenaming, isDeleting } =
    useHistory();

  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(conversation.title);

  const inputRef = useRef<HTMLInputElement>(null);

  const snippet = getSnippet(conversation.messages);

  function startRename() {
    setEditTitle(conversation.title);
    setIsEditing(true);
  }

  function saveRename() {
    const trimmed = editTitle.trim();
    if (!trimmed) {
      setEditTitle(conversation.title);
      setIsEditing(false);
      return;
    }
    if (trimmed !== conversation.title) {
      renameConversation(conversation.id, trimmed);
    }
    setIsEditing(false);
  }

  function cancelRename() {
    setEditTitle(conversation.title);
    setIsEditing(false);
  }

  function handleInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      saveRename();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancelRename();
    }
  }

  return (
    <div
      className={cn(
        "group relative w-full rounded-card border border-aurum-border bg-aurum-surface p-4 text-left shadow-card",
        "transition hover:border-aurum-accent focus-within:ring-2 focus-within:ring-aurum-accent/50",
        isDeleting && "opacity-60"
      )}
    >
      <button
        type="button"
        onClick={onClick}
        disabled={isDeleting}
        className="w-full text-left disabled:cursor-not-allowed"
        aria-label={
          isEditing ? undefined : t("open", { title: conversation.title })
        }
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3 pr-8">
            {agent && <Badge color={agent.badgeColor}>{agent.badge}</Badge>}
            {isEditing ? (
              <span
                className="w-full"
                onClick={(e) => e.stopPropagation()}
                onDoubleClick={(e) => e.stopPropagation()}
              >
                <input
                  ref={inputRef}
                  type="text"
                  value={editTitle}
                  onChange={(e) => setEditTitle(e.target.value)}
                  onBlur={saveRename}
                  onKeyDown={handleInputKeyDown}
                  disabled={isRenaming}
                  autoFocus
                  aria-label={t("titleLabel")}
                  className={cn(
                    "w-full rounded border border-aurum-border bg-aurum-bg px-2 py-1 text-base font-semibold text-aurum-text",
                    "focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
                    isRenaming && "opacity-60"
                  )}
                />
              </span>
            ) : (
              <h3
                className="truncate text-base font-semibold text-aurum-text"
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  startRename();
                }}
              >
                {conversation.title}
              </h3>
            )}
          </div>
          <div className="flex shrink-0 flex-col items-end gap-0.5 pr-9 text-right">
            <span className="text-xs text-aurum-text-3">
              {formatShortDate(conversation.lastMessageAt, locale)}
            </span>
            <span className="text-xs text-aurum-text-3">
              {conversation.time}
            </span>
          </div>
        </div>

        {!isEditing && (
          <>
            {snippet && (
              <p className="mt-2 line-clamp-2 text-sm text-aurum-text-2">
                {snippet}
              </p>
            )}

            <div className="mt-3 flex items-center gap-1.5 text-xs text-aurum-text-3">
              <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
              <span>{t("messageCount", { count: conversation.messageCount })}</span>
            </div>
          </>
        )}
      </button>

      <div className="absolute right-4 top-4">
        <ConversationActionsMenu
          conversationTitle={conversation.title}
          isDeleting={isDeleting}
          onRename={startRename}
          onDelete={() => deleteConversation(conversation.id)}
        />
      </div>
    </div>
  );
}
