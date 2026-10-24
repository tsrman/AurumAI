"use client";

import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

export interface ModalProps {
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: "md" | "lg" | "xl";
  /** Content between the title and the scrolling body, e.g. tabs. */
  header?: React.ReactNode;
}

const SIZES = { md: "max-w-lg", lg: "max-w-2xl", xl: "max-w-4xl" };

/** Centered dialog with a fixed header and footer and a scrolling body. */
export function Modal({ open, title, description, onClose, children, footer, size = "lg", header }: ModalProps) {
  const t = useTranslations("common");
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    function handleKeyDown(event: KeyboardEvent) {
      // A nested confirmation handles its own Escape first.
      if (event.key === "Escape" && !document.querySelector('[role="alertdialog"]')) {
        event.preventDefault();
        onClose();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <>
      <div className="fixed inset-0 z-30 bg-black/40" onClick={onClose} aria-hidden="true" />
      <div className="fixed inset-0 z-30 flex items-center justify-center p-4" onClick={onClose}>
        <div
          ref={panelRef}
          tabIndex={-1}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          onClick={(event) => event.stopPropagation()}
          className={cn(
            "flex max-h-[90vh] w-full flex-col rounded-card border border-aurum-border bg-aurum-surface shadow-drawer outline-none",
            SIZES[size]
          )}
        >
          <div className="flex items-start justify-between gap-4 px-5 pt-5">
            <div className="min-w-0">
              <h2 id={titleId} className="text-base font-semibold text-aurum-text">{title}</h2>
              {description ? <p className="mt-1 text-sm text-aurum-text-3">{description}</p> : null}
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label={t("close")}
              className="rounded-btn p-1 text-aurum-text-3 transition hover:bg-aurum-surface-3 hover:text-aurum-text"
            >
              <X className="h-5 w-5" aria-hidden="true" />
            </button>
          </div>
          {header ? <div className="border-b border-aurum-border-soft px-5 pt-4">{header}</div> : <div className="pt-4" />}
          <div className="flex-1 overflow-y-auto px-5 py-5">{children}</div>
          {footer ? (
            <div className="flex flex-wrap justify-end gap-2 border-t border-aurum-border-soft px-5 py-4">{footer}</div>
          ) : null}
        </div>
      </div>
    </>,
    document.body
  );
}
