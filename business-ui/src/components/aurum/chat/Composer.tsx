"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Plus, ArrowUp, Square, Paperclip, SlidersHorizontal, X } from "lucide-react";
import { IconButton } from "@/components/primitives/IconButton";
import { useAppShell } from "@/hooks/useAppShell";
import { useChatAgents } from "@/hooks/useChatAgents";
import { AgentPickerMenu } from "./AgentPickerMenu";
import { ModelPickerMenu } from "./ModelPickerMenu";
import { ModelSettingsDialog } from "./ModelSettingsDialog";
import { WorkflowPickerMenu, type ChatMode } from "./WorkflowPickerMenu";
import type { ChatAgentId } from "@/lib/chat-api";
import { useAuth } from "@/providers/AuthProvider";
import { ACCEPTED_SUFFIXES, MAX_FILE_BYTES } from "@/components/aurum/knowledge/DragZone";
import { useAttachFile, useDocuments } from "@/hooks/useDocuments";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";
import { deleteConversationImage, uploadConversationImage } from "@/lib/images-api";
import { deleteConversationFile, uploadConversationFile, type AttachedFile } from "@/lib/files-api";

/** Match the backend's validated image formats and IMAGES_UPLOAD_MAX_BYTES. */
const IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".webp"] as const;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_IMAGES_PER_MESSAGE = 4;
/** Files Hermes Agent works on in its workspace (business-backend's conversation file types). */
const HERMES_SUFFIXES = [".xlsx", ".csv", ".txt", ".md", ".json", ".pdf", ".docx", ".pptx", ".zip"] as const;
const MAX_HERMES_FILE_BYTES = 20 * 1024 * 1024;
const MAX_FILES_PER_MESSAGE = 10;

interface PendingImage {
  imageId: string;
  name: string;
  previewUrl: string;
}

export interface ComposerProps {
  onSend?: (
    text: string,
    workflowId?: string,
    imageIds?: string[],
    agent?: ChatAgentId,
    files?: AttachedFile[]
  ) => void;
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
  const { agents, myAgents, activeAgentId, conversations, models, activeModelId } = useAppShell();
  const visionModel = Boolean(models.find((model) => model.id === activeModelId)?.vision);
  // An agent stays chosen for the conversation, whose workspace it keeps,
  // also after a reload: the conversation names it. A workflow is chosen for
  // one turn. A new chat's first send creates its conversation, which keeps
  // the choice.
  const { data: chatAgents = [] } = useChatAgents();
  const [choice, setChoice] = useState<{ conversationId: string | null; mode: ChatMode } | null>(null);
  if (choice && choice.conversationId == null && conversationId != null) {
    setChoice({ conversationId, mode: choice.mode });
  }
  const conversationAgent = conversations.find((c) => c.id === conversationId)?.agent;
  const mode: ChatMode =
    choice && choice.conversationId === (conversationId ?? null)
      ? choice.mode
      : conversationAgent === "hermes" && chatAgents.some((item) => item.id === "hermes")
        ? { kind: "agent", agent: "hermes" }
        : { kind: "chat" };
  const setMode = useCallback(
    (next: ChatMode) => setChoice({ conversationId: conversationId ?? null, mode: next }),
    [conversationId]
  );
  const workflowId = mode.kind === "workflow" ? mode.workflowId : null;
  const agent = mode.kind === "agent" ? mode.agent : undefined;
  // A workflow's agents use their own models; the backend inlines images
  // only for those marked as vision models. Hermes takes no images yet.
  const imagesAllowed = !agent && (visionModel || Boolean(workflowId));
  const { isAuthenticated } = useAuth();
  const [text, setText] = useState("");
  const [settingsConversationId, setSettingsConversationId] = useState<
    string | null
  >(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [attachedKnowledgeBaseId, setAttachedKnowledgeBaseId] = useState<string | null>(null);
  // Uploaded images waiting for the next message.
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);
  // Files for Hermes Agent's workspace, waiting for the next message.
  const [pendingFiles, setPendingFiles] = useState<AttachedFile[]>([]);
  const hermes = agent === "hermes";
  const previewUrlsRef = useRef<string[]>([]);
  useEffect(() => {
    const urls = previewUrlsRef.current;
    return () => urls.forEach((url) => URL.revokeObjectURL(url));
  }, []);

  // A different conversation (or none) means any chip below belongs to a
  // conversation that's no longer active — reset in render, not an effect
  // (https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes).
  const [lastSeenConversationId, setLastSeenConversationId] = useState(conversationId ?? null);
  if ((conversationId ?? null) !== lastSeenConversationId) {
    setLastSeenConversationId(conversationId ?? null);
    setAttachedKnowledgeBaseId(null);
    setAttachError(null);
    setPendingImages([]);
    setPendingFiles([]);
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
      const images: File[] = [];
      const workspace: File[] = [];
      for (const file of files) {
        const name = file.name.toLowerCase();
        if (hermes) {
          // Hermes works on files in its workspace instead of searching them.
          if (!HERMES_SUFFIXES.some((suffix) => name.endsWith(suffix))) {
            rejections.push(t("unsupported", { file: file.name }));
          } else if (file.size > MAX_HERMES_FILE_BYTES) {
            rejections.push(t("tooLarge", { file: file.name }));
          } else if (pendingFiles.length + workspace.length >= MAX_FILES_PER_MESSAGE) {
            rejections.push(t("tooManyFiles", { max: MAX_FILES_PER_MESSAGE }));
          } else {
            workspace.push(file);
          }
        } else if (IMAGE_SUFFIXES.some((suffix) => name.endsWith(suffix))) {
          if (!imagesAllowed) {
            rejections.push(t("imagesNotAccepted", { file: file.name }));
          } else if (file.size > MAX_IMAGE_BYTES) {
            rejections.push(t("imageTooLarge", { file: file.name }));
          } else if (pendingImages.length + images.length >= MAX_IMAGES_PER_MESSAGE) {
            rejections.push(t("tooManyImages", { max: MAX_IMAGES_PER_MESSAGE }));
          } else {
            images.push(file);
          }
        } else if (!ACCEPTED_SUFFIXES.some((suffix) => name.endsWith(suffix))) {
          rejections.push(t("unsupported", { file: file.name }));
        } else if (file.size > MAX_FILE_BYTES) {
          rejections.push(t("tooLarge", { file: file.name }));
        } else {
          accepted.push(file);
        }
      }

      const failures: string[] = [...rejections];

      if (accepted.length > 0 || images.length > 0 || workspace.length > 0) {
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
          for (const file of images) {
            try {
              const uploaded = await uploadConversationImage(id, file);
              const previewUrl = URL.createObjectURL(file);
              previewUrlsRef.current.push(previewUrl);
              setPendingImages((prev) => [
                ...prev,
                { imageId: uploaded.imageId, name: file.name, previewUrl },
              ]);
            } catch (err) {
              failures.push(
                t("attachFailedFor", {
                  file: file.name,
                  reason: err instanceof Error ? errorMessage(err) : t("attachFailedReason"),
                })
              );
            }
          }
          for (const file of workspace) {
            try {
              const uploaded = await uploadConversationFile(id, file);
              setPendingFiles((prev) => [...prev, uploaded]);
            } catch (err) {
              failures.push(
                t("attachFailedFor", {
                  file: file.name,
                  reason: err instanceof Error ? errorMessage(err) : t("attachFailedReason"),
                })
              );
            }
          }
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
    [
      attachFile,
      conversationId,
      ensureConversationId,
      isDisabled,
      t,
      errorMessage,
      imagesAllowed,
      pendingImages.length,
      hermes,
      pendingFiles.length,
    ]
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
    if (pendingImages.length > 0) {
      if (!imagesAllowed) return;
      onSend?.(
        trimmed,
        workflowId ?? undefined,
        pendingImages.map((image) => image.imageId)
      );
      setPendingImages([]);
    } else if (hermes && pendingFiles.length > 0) {
      onSend?.(trimmed, undefined, undefined, agent, pendingFiles);
      setPendingFiles([]);
    } else if (!trimmed) {
      return;
    } else if (workflowId) {
      onSend?.(trimmed, workflowId);
    } else if (agent) {
      onSend?.(trimmed, undefined, undefined, agent);
    } else {
      onSend?.(trimmed);
    }
    // A workflow is chosen for one turn: then the conversation's own agent again.
    if (workflowId) setChoice(null);
    setText("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [text, onSend, isStreaming, isDisabled, workflowId, agent, pendingImages, imagesAllowed, hermes, pendingFiles]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const imagesBlocked = pendingImages.length > 0 && !imagesAllowed;
  const canSend =
    !isStreaming &&
    !isDisabled &&
    !imagesBlocked &&
    (text.trim().length > 0 || pendingImages.length > 0 || (hermes && pendingFiles.length > 0));

  return (
    <div className="flex-none px-6 pb-4 pt-1.5">
      <div className="mx-auto max-w-[824px]">
        {(attachedDocuments && attachedDocuments.length > 0) ||
        attachError ||
        pendingImages.length > 0 ||
        pendingFiles.length > 0 ? (
          <div className="mb-2 flex flex-wrap gap-2">
            {pendingFiles.map((file) => (
              <span
                key={file.fileId}
                className="inline-flex items-center gap-1.5 rounded-full border border-aurum-border bg-aurum-surface px-2.5 py-1 text-xs text-aurum-text-2"
              >
                <Paperclip className="h-3 w-3" />
                {file.name}
                <button
                  type="button"
                  aria-label={t("removeFile", { file: file.name })}
                  onClick={() => {
                    setPendingFiles((prev) => prev.filter((item) => item.fileId !== file.fileId));
                    // Best effort: a leftover upload is harmless, just unused.
                    if (conversationId) {
                      void deleteConversationFile(conversationId, file.fileId).catch(() => undefined);
                    }
                  }}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            {pendingImages.map((image) => (
              <span
                key={image.imageId}
                className="relative inline-flex h-16 w-16 overflow-hidden rounded-card border border-aurum-border bg-aurum-surface"
              >
                {/* eslint-disable-next-line @next/next/no-img-element -- local preview of the user's own file */}
                <img src={image.previewUrl} alt={image.name} className="h-full w-full object-cover" />
                <button
                  type="button"
                  aria-label={t("removeImage", { file: image.name })}
                  onClick={() => {
                    setPendingImages((prev) => prev.filter((item) => item.imageId !== image.imageId));
                    // Best effort: a leftover upload is harmless, just unused.
                    if (conversationId) {
                      void deleteConversationImage(conversationId, image.imageId).catch(() => undefined);
                    }
                  }}
                  className="absolute right-0.5 top-0.5 inline-flex h-5 w-5 items-center justify-center rounded-full bg-aurum-surface/90 text-aurum-text-2 shadow-card hover:text-aurum-text"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            {imagesBlocked ? (
              <span
                role="alert"
                className="inline-flex items-center rounded-full border border-aurum-err-fg/30 bg-aurum-err-bg px-2.5 py-1 text-xs text-aurum-err-fg"
              >
                {t("modelRejectsImages")}
              </span>
            ) : null}
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
              accept={(hermes
                ? HERMES_SUFFIXES
                : imagesAllowed
                  ? [...ACCEPTED_SUFFIXES, ...IMAGE_SUFFIXES]
                  : ACCEPTED_SUFFIXES
              ).join(",")}
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
              value={mode}
              onChange={setMode}
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
