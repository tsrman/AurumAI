"use client";

import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { Button } from "./Button";
import { useTranslations } from "next-intl";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** App-styled replacement for `window.confirm()` — native browser dialogs
 * can't be styled, block automated testing/screenshots, and read as jarring
 * next to the rest of the UI. */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel,
  destructive = false,
  confirmDisabled = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const t = useTranslations("common");
  const titleId = useId();
  const confirmButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) confirmButtonRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onCancel]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <>
      <div
        className="fixed inset-0 z-40 bg-black/40"
        onClick={onCancel}
        aria-hidden="true"
      />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          role="alertdialog"
          aria-modal="true"
          aria-labelledby={titleId}
          className="w-full max-w-sm rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-drawer"
          onClick={(e) => e.stopPropagation()}
        >
          <h2 id={titleId} className="text-base font-semibold text-aurum-text">
            {title}
          </h2>
          {description && (
            <p className="mt-2 text-sm text-aurum-text-2">{description}</p>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <Button type="button" variant="secondary" onClick={onCancel}>
              {cancelLabel ?? t("cancel")}
            </Button>
            <Button
              ref={confirmButtonRef}
              type="button"
              variant={destructive ? "danger" : "primary"}
              disabled={confirmDisabled}
              onClick={onConfirm}
            >
              {confirmLabel ?? t("confirm")}
            </Button>
          </div>
        </div>
      </div>
    </>,
    document.body
  );
}
