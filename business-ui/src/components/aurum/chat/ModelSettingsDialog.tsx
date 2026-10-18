"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button } from "@/components/primitives/Button";
import { Slider } from "@/components/primitives/Slider";
import { useErrorMessage } from "@/lib/errors";
import { useUpdateConversation } from "@/hooks/useUpdateConversation";
import { useAppShell } from "@/hooks/useAppShell";
import { useTranslations } from "next-intl";

export interface ModelSettingsDialogProps {
  open: boolean;
  conversationId: string;
  temperature: number;
  onClose: () => void;
}

/** Small conversation-scoped override for the LLM temperature — otherwise
 * inherited from the agent/organization defaults, resolved server-side. */
export function ModelSettingsDialog({
  open,
  conversationId,
  temperature,
  onClose,
}: ModelSettingsDialogProps) {
  const t = useTranslations("chat.pickers.modelSettings");
  const errorMessage = useErrorMessage();
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const { setConversations } = useAppShell();
  const update = useUpdateConversation();
  const [draft, setDraft] = useState(temperature);
  // Reset the draft to the persisted value each time the dialog opens —
  // done during render (not an effect) since it's adjusting state in
  // response to a prop change, matching Composer.tsx's own
  // lastSeenConversationId pattern.
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setDraft(temperature);
  }

  useEffect(() => {
    if (open) closeButtonRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open || typeof document === "undefined") return null;

  const handleSave = () => {
    update.mutate(
      { id: conversationId, temperature: draft },
      {
        onSuccess: (response) => {
          setConversations((prev) =>
            prev.map((conv) =>
              conv.id === conversationId
                ? { ...conv, temperature: response.temperature }
                : conv
            )
          );
          onClose();
        },
      }
    );
  };

  return createPortal(
    <>
      <div
        className="fixed inset-0 z-40 bg-black/40"
        onClick={onClose}
        aria-hidden="true"
      />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          className="w-full max-w-sm rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-drawer"
          onClick={(e) => e.stopPropagation()}
        >
          <h2 id={titleId} className="text-base font-semibold text-aurum-text">
            {t("title")}
          </h2>
          <div className="mt-4">
            <div className="mb-1 flex items-center justify-between">
              <label
                htmlFor="conversation-temperature"
                className="text-sm font-medium text-aurum-text-2"
              >
                {t("temperature")}
              </label>
              <span className="text-sm tabular-nums text-aurum-text">
                {draft.toFixed(1)}
              </span>
            </div>
            <Slider
              id="conversation-temperature"
              min={0}
              max={2}
              step={0.1}
              value={draft}
              onChange={setDraft}
            />
            <p className="mt-2 text-sm text-aurum-text-2">
              {t("hint")}
            </p>
          </div>
          {update.isError ? (
            <p className="mt-3 text-sm text-aurum-err-fg">
              {errorMessage(update.error)}
            </p>
          ) : null}
          <div className="mt-5 flex justify-end gap-2">
            <Button
              ref={closeButtonRef}
              type="button"
              variant="secondary"
              onClick={onClose}
            >
              {t("cancel")}
            </Button>
            <Button
              type="button"
              variant="primary"
              disabled={update.isPending}
              onClick={handleSave}
            >
              {update.isPending ? t("saving") : t("save")}
            </Button>
          </div>
        </div>
      </div>
    </>,
    document.body
  );
}
