"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Plus, ArrowUp, Square, Paperclip, SlidersHorizontal, X } from "lucide-react";
import { IconButton } from "@/components/primitives/IconButton";
import { useAppShell } from "@/hooks/useAppShell";
import { AgentPickerMenu } from "./AgentPickerMenu";
import { ModelPickerMenu } from "./ModelPickerMenu";
import { ModelSettingsDialog } from "./ModelSettingsDialog";
import { WorkflowPickerMenu } from "./WorkflowPickerMenu";
import { useAuth } from "@/providers/AuthProvider";
import { ACCEPTED_SUFFIXES, MAX_FILE_BYTES } from "@/components/aurum/knowledge/DragZone";
import { useAttachFile, useDocuments } from "@/hooks/useDocuments";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

export interface ComposerProps {
  onSend?: (text: string, workflowId?: string) => void;
  isStreaming?: boolean;
  onStop?: () => void;
  // while Keycloak is still initializing, isRealMode()
  // in useChat.ts reads false (no window.keycloak.token yet), so a message
  // sent before then silently goes through the mock chat path instead of a
  // real request — and that mock conversation later 422s once real mode
  // kicks in. True while useAuth().isLoading, from the page that renders
  // this — not derived here, Composer has no auth context of its own.
  disabled?: boolean;
  blockedByActiveTurn?: boolean;
  // null until the first message is sent or a file is attached — the
  // attach button creates an empty conversation via ensureConversationId
  // before the user has typed anything, same as useChat's real send path
  // already does lazily on first message.
  conversationId?: string | null;
  ensureConversationId?: () => Promise<string>;
}

const MAX_HEIGHT = 200;

export function Composer({
  onSend,
  isStreaming,
  onStop,
  disabled,
  blockedByActiveTurn = false,
  conversationId,
  ensureConversationId,
}: ComposerProps) {
  const t = useTranslations("chat.composer");
  const errorMessage = useErrorMessage();
  const isDisabled = Boolean(disabled || blockedByActiveTurn);
  const { agents, myAgents, activeAgentId, conversations } = useAppShell();
  const { isAuthenticated } = useAuth();
  const [text, setText] = useState("");
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [settingsConversationId, setSettingsConversationId] = useState<
    string | null
  >(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [attachedKnowledgeBaseId, setAttachedKnowledgeBaseId] = useState<string | null>(null);

  // A different conversation (or none) means any chip below belongs to a
  // conversation that's no longer active — reset in render, not an effect
  // (https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes).
  const [lastSeenConversationId, setLastSeenConversationId] = useState(conversationId ?? null);
  if ((conversationId ?? null) !== lastSeenConversationId) {
    setLastSeenConversationId(conversationId ?? null);
    setAttachedKnowledgeBaseId(null);
    setAttachError(null);
  }

  const attachFile = useAttachFile();
  const { data: attachedDocuments } = useDocuments(attachedKnowledgeBaseId ?? "", {
    enabled: Boolean(attachedKnowledgeBaseId),
  });

  const handleAttachClick = useCallback(() => {
    if (isStreaming || isDisabled) return;
    fileInputRef.current?.click();
  }, [isStreaming, isDisabled]);

  const handleFileSelected = useCallback(
    async (event: React.ChangeEvent<HTMLInputElement>) => {
      if (isDisabled) return;
      const files = Array.from(event.target.files ?? []);
      event.target.value = "";
      if (files.length === 0) return;

      const rejections: string[] = [];
      const accepted: File[] = [];
      for (const file of files) {
        const name = file.name.toLowerCase();
        if (!ACCEPTED_SUFFIXES.some((suffix) => name.endsWith(suffix))) {
          rejections.push(t("unsupported", { file: file.name }));
        } else if (file.size > MAX_FILE_BYTES) {
          rejections.push(t("tooLarge", { file: file.name }));
        } else {
          accepted.push(file);
        }
      }

      const failures: string[] = [...rejections];

      if (accepted.length > 0) {
        try {
          // Resolved once for the whole batch — ensureConversationId creates
          // a conversation on first call, and every file in this selection
          // must land in that same one, not one each.
          const id = conversationId ?? (await ensureConversationId?.());
          if (!id) throw new Error(t("noConversation"));

          // Sequential, not Promise.all: the backend lazily provisions this
          // conversation's hidden knowledge base on the *first* attachment —
          // concurrent first calls could race (see
          // conversation_attachments.py's _get_or_create_conversation_knowledge_base).
          for (const file of accepted) {
            try {
              const doc = await attachFile.mutateAsync({ conversationId: id, file });
              setAttachedKnowledgeBaseId(doc.knowledge_base_id);
            } catch (err) {
              failures.push(
                t("attachFailedFor", {
                  file: file.name,
                  reason: err instanceof Error ? errorMessage(err) : t("attachFailedReason"),
                })
              );
            }
          }
        } catch (err) {
          failures.push(err instanceof Error ? errorMessage(err) : t("attachFailed"));
        }
      }

      setAttachError(failures.length > 0 ? failures.join("; ") : null);
    },
    [attachFile, conversationId, ensureConversationId, isDisabled, t, errorMessage]
  );

  const handleOpenSettingsClick = useCallback(async () => {
    if (isDisabled) return;
    const id = conversationId ?? (await ensureConversationId?.());
    if (!id) return;
    setSettingsConversationId(id);
  }, [conversationId, ensureConversationId, isDisabled]);

  const settingsConversation = useMemo(
    () => conversations.find((c) => c.id === settingsConversationId) ?? null,
    [conversations, settingsConversationId]
  );

  const activeAgent = useMemo(
    () => [...agents, ...myAgents].find((a) => a.id === activeAgentId),
    [agents, myAgents, activeAgentId]
  );

  const placeholder = blockedByActiveTurn
    ? t("answerQuestionFirst")
    : activeAgent
      ? t("placeholderAgent", { agent: activeAgent.name })
      : t("placeholder");

  const resize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, []);

  useEffect(() => {
    resize();
  }, [text, resize]);

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
  };

  const handleSend = useCallback(() => {
    // The Send button already swaps for Stop while streaming (below), but
    // the textarea's Enter key had no equivalent guard — it kept sending
    // a second message mid-stream, leaving the first assistant message
    // stuck mid-phase forever.
    if (isStreaming || isDisabled) return;
    const trimmed = text.trim();
    if (!trimmed) return;
    if (workflowId) {
      onSend?.(trimmed, workflowId);
    } else {
      onSend?.(trimmed);
    }
    setWorkflowId(null);
    setText("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [text, onSend, isStreaming, isDisabled, workflowId]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const canSend = !isStreaming && !isDisabled && text.trim().length > 0;

  return (
    <div className="flex-none px-6 pb-4 pt-1.5">
      <div className="mx-auto max-w-[824px]">
        {(attachedDocuments && attachedDocuments.length > 0) || attachError ? (
          <div className="mb-2 flex flex-wrap gap-2">
            {attachedDocuments?.map((doc) => (
              <span
                key={doc.id}
                className="inline-flex items-center gap-1.5 rounded-full border border-aurum-border bg-aurum-surface px-2.5 py-1 text-xs text-aurum-text-2"
              >
                <Paperclip className="h-3 w-3" />
                {doc.title}
                <span className="text-aurum-text-3">
                  {doc.status === "pending" || doc.status === "indexing"
                    ? t("attachmentProcessing")
                    : doc.status === "error"
                      ? t("attachmentError")
                      : t("attachmentReady")}
                </span>
              </span>
            ))}
            {attachError && (
              <span
                role="alert"
                className="inline-flex items-center gap-1.5 rounded-full border border-aurum-err-fg/30 bg-aurum-err-bg px-2.5 py-1 text-xs text-aurum-err-fg"
              >
                {attachError}
                <button
                  type="button"
                  aria-label={t("hide")}
                  onClick={() => setAttachError(null)}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            )}
          </div>
        ) : null}
        <div className="rounded-composer border border-aurum-border bg-aurum-surface px-[15px] py-[13px] shadow-[0_6px_22px_rgba(20,30,60,0.07)] transition-colors focus-within:border-[#d8b061]">
          <textarea
            ref={textareaRef}
            value={text}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            aria-label={t("message")}
            rows={1}
            disabled={isDisabled}
            className="block max-h-[200px] w-full resize-none bg-transparent text-[15px] leading-[1.5] text-aurum-text outline-none placeholder:text-aurum-muted disabled:opacity-60"
          />
          <div className="mt-2.5 flex items-center gap-2">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              aria-label={t("chooseFile")}
              accept={ACCEPTED_SUFFIXES.join(",")}
              className="hidden"
              onChange={handleFileSelected}
            />
            <IconButton
              aria-label={t("attach")}
              title={t("attach")}
              className="h-[34px] w-[34px]"
              disabled={isDisabled || isStreaming}
              onClick={handleAttachClick}
            >
              <Plus className="h-4 w-4" />
            </IconButton>

            <AgentPickerMenu />

            <IconButton
              aria-label={t("modelSettings")}
              title={t("modelSettings")}
              className="h-[34px] w-[34px]"
              disabled={isDisabled || isStreaming}
              onClick={handleOpenSettingsClick}
            >
              <SlidersHorizontal className="h-4 w-4" />
            </IconButton>

            <WorkflowPickerMenu
              value={workflowId}
              onChange={setWorkflowId}
              disabled={isDisabled || isStreaming || !isAuthenticated}
            />

            <div className="flex-1" />

            <ModelPickerMenu />

            {isStreaming ? (
              <IconButton
                aria-label={t("stop")}
                title={t("stop")}
                onClick={onStop}
                className="border-aurum-accent bg-aurum-accent text-aurum-accent-ink hover:bg-aurum-accent hover:text-aurum-accent-ink hover:brightness-105"
              >
                <Square className="h-[14px] w-[14px]" fill="currentColor" />
              </IconButton>
            ) : (
              <IconButton
                aria-label={t("send")}
                title={t("send")}
                onClick={handleSend}
                disabled={!canSend}
                className="border-aurum-accent bg-aurum-accent text-aurum-accent-ink hover:bg-aurum-accent hover:text-aurum-accent-ink hover:brightness-105 disabled:opacity-40"
              >
                <ArrowUp className="h-[17px] w-[17px]" />
              </IconButton>
            )}
          </div>
        </div>
      </div>
      {settingsConversationId ? (
        <ModelSettingsDialog
          open={settingsConversationId !== null}
          conversationId={settingsConversationId}
          temperature={settingsConversation?.temperature ?? 0.7}
          onClose={() => setSettingsConversationId(null)}
        />
      ) : null}
    </div>
  );
}
