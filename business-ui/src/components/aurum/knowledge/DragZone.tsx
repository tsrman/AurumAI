"use client";

import { useCallback, useRef, useState } from "react";
import { Upload } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslations } from "next-intl";

/** Match the backend DOCUMENT_UPLOAD_ALLOWED_SUFFIXES formats. */
export const ACCEPTED_SUFFIXES = [
  ".pdf",
  ".docx",
  ".doc",
  ".pptx",
  ".html",
  ".htm",
  ".md",
  ".txt",
] as const;

/** Match the backend DOCUMENT_UPLOAD_MAX_BYTES limit. */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;

export interface DragZoneProps {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
}

function isAccepted(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED_SUFFIXES.some((suffix) => name.endsWith(suffix));
}

export function DragZone({ onFiles, disabled = false }: DragZoneProps) {
  const t = useTranslations("knowledge.upload");
  const [isDragging, setIsDragging] = useState(false);
  const [rejected, setRejected] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const accept = useCallback(
    (files: FileList | null) => {
      setRejected(null);
      const all = Array.from(files ?? []);
      if (all.length === 0) return;

      // Reject immediately so users need not wait for a server415/413
      // to learn that the selected file is unsupported or too large.
      const wrongType = all.filter((f) => !isAccepted(f));
      const tooBig = all.filter((f) => isAccepted(f) && f.size > MAX_FILE_BYTES);
      const good = all.filter((f) => isAccepted(f) && f.size <= MAX_FILE_BYTES);

      if (wrongType.length > 0) {
        setRejected(
          t("unsupported", { files: wrongType.map((f) => f.name).join(", ") })
        );
      } else if (tooBig.length > 0) {
        setRejected(t("tooLarge", { files: tooBig.map((f) => f.name).join(", ") }));
      }

      if (good.length > 0) onFiles(good);
    },
    [onFiles, t]
  );

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setIsDragging(false);
      if (!disabled) accept(event.dataTransfer.files);
    },
    [accept, disabled]
  );

  const openPicker = useCallback(() => {
    if (!disabled) inputRef.current?.click();
  }, [disabled]);

  return (
    <div className="flex flex-col gap-2">
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label={t("zone")}
        aria-disabled={disabled}
        onClick={openPicker}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setIsDragging(true);
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          setIsDragging(false);
        }}
        onDrop={handleDrop}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openPicker();
          }
        }}
        className={cn(
          "flex flex-col items-center justify-center gap-3 rounded-card border-2 border-dashed px-6 py-8 transition",
          "border-aurum-border bg-aurum-surface text-aurum-text-2",
          disabled
            ? "cursor-not-allowed opacity-60"
            : "cursor-pointer hover:border-aurum-accent hover:bg-aurum-surface-2",
          "focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
          isDragging && !disabled && "border-aurum-accent bg-aurum-surface-2"
        )}
      >
        <Upload className="h-8 w-8 text-aurum-text-3" aria-hidden="true" />
        <div className="text-center">
          <p className="text-sm font-medium text-aurum-text">
            {disabled
              ? t("uploading")
              : t("prompt")}
          </p>
          <p className="mt-1 text-xs text-aurum-text-3">
            {t("formats")}
          </p>
        </div>
      </div>

      <input
        ref={inputRef}
        type="file"
        multiple
        aria-label={t("choose")}
        accept={ACCEPTED_SUFFIXES.join(",")}
        className="hidden"
        onChange={(event) => {
          accept(event.target.files);
          // Reset the input so selecting the same file again still triggers
          // onChange.
          event.target.value = "";
        }}
      />

      {rejected && (
        <p role="alert" className="text-xs text-aurum-err-fg">
          {rejected}
        </p>
      )}
    </div>
  );
}
