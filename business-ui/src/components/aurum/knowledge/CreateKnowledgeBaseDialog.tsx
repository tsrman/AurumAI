"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { Button } from "@/components/primitives/Button";
import { Field } from "@/components/primitives/Field";
import { Textarea } from "@/components/primitives/Textarea";
import { useTranslations } from "next-intl";

export interface CreateKnowledgeBaseDialogProps {
  open: boolean;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onCreate: (name: string, description: string) => void | Promise<void>;
}

export function CreateKnowledgeBaseDialog({
  open,
  pending,
  error,
  onCancel,
  onCreate,
}: CreateKnowledgeBaseDialogProps) {
  const t = useTranslations("knowledge.create");
  const titleId = React.useId();
  const nameRef = React.useRef<HTMLInputElement>(null);
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [localError, setLocalError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    nameRef.current?.focus();
  }, [open]);

  React.useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onCancel]);

  if (!open || typeof document === "undefined") return null;

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) {
      setLocalError(t("nameRequired"));
      return;
    }
    setLocalError(null);
    void onCreate(trimmedName, description.trim());
  };

  return createPortal(
    <>
      <div
        className="fixed inset-0 z-40 bg-black/40"
        aria-hidden="true"
        onClick={onCancel}
      />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          className="w-full max-w-lg rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-drawer"
          onClick={(event) => event.stopPropagation()}
        >
          <h2 id={titleId} className="text-base font-semibold text-aurum-text">
            {t("title")}
          </h2>
          <form className="mt-4 space-y-4" onSubmit={handleSubmit}>
            <Field
              ref={nameRef}
              label={t("name")}
              value={name}
              disabled={pending}
              onChange={(event) => setName(event.target.value)}
            />
            <Textarea
              label={t("description")}
              rows={4}
              value={description}
              disabled={pending}
              onChange={(event) => setDescription(event.target.value)}
            />
            {localError || error ? (
              <p role="alert" className="text-sm text-aurum-err-fg">
                {localError ?? error}
              </p>
            ) : null}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" disabled={pending} onClick={onCancel}>
                {t("cancel")}
              </Button>
              <Button type="submit" disabled={pending}>
                {pending ? t("creating") : t("create")}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </>,
    document.body
  );
}
