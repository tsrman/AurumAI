"use client";

import { useState } from "react";
import { Download, FileArchive, FileSpreadsheet, FileText, Loader2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useAppShell } from "@/hooks/useAppShell";
import { getConversationFileBlob, type AttachedFile } from "@/lib/files-api";

function FileIcon({ contentType }: { contentType: string }) {
  const Icon =
    contentType === "text/csv" || contentType.includes("spreadsheet")
      ? FileSpreadsheet
      : contentType === "application/zip"
        ? FileArchive
        : FileText;
  return <Icon className="h-5 w-5 shrink-0 text-aurum-text-3" aria-hidden="true" />;
}

function FileCard({ conversationId, file }: { conversationId: string; file: AttachedFile }) {
  const t = useTranslations("chat.files");
  const format = useFormatter();
  const [state, setState] = useState<"idle" | "downloading" | "failed">("idle");
  const size =
    file.sizeBytes >= 1024 * 1024
      ? format.number(file.sizeBytes / (1024 * 1024), { style: "unit", unit: "megabyte", maximumFractionDigits: 1 })
      : format.number(Math.max(file.sizeBytes / 1024, 0.1), {
          style: "unit",
          unit: "kilobyte",
          maximumFractionDigits: 1,
        });

  async function download() {
    setState("downloading");
    try {
      const blob = await getConversationFileBlob(conversationId, file.fileId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Let the browser start the download before the URL goes away.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setState("idle");
    } catch {
      setState("failed");
    }
  }

  return (
    <li className="flex items-center gap-3 rounded-card border border-aurum-border bg-aurum-surface-2 px-3 py-2">
      <FileIcon contentType={file.contentType} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-aurum-text" title={file.name}>
          {file.name}
        </p>
        <p className="text-xs text-aurum-text-3">
          {state === "failed" ? <span role="alert">{t("failed")}</span> : size}
        </p>
      </div>
      <button
        type="button"
        onClick={download}
        disabled={state === "downloading"}
        aria-label={t("download", { name: file.name })}
        className="inline-flex h-8 w-8 items-center justify-center rounded-btn text-aurum-text-2 transition hover:bg-aurum-surface hover:text-aurum-text disabled:opacity-60"
      >
        {state === "downloading" ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : (
          <Download className="h-4 w-4" aria-hidden="true" />
        )}
      </button>
    </li>
  );
}

/** Files an answer's agent attached, downloaded with the user's session. */
export function ConversationFiles({ files }: { files: AttachedFile[] }) {
  const t = useTranslations("chat.files");
  const { activeConversationId } = useAppShell();
  if (!activeConversationId || files.length === 0) return null;
  return (
    <ul aria-label={t("label")} className="flex max-w-md flex-col gap-2">
      {files.map((file) => (
        <FileCard key={file.fileId} conversationId={activeConversationId} file={file} />
      ))}
    </ul>
  );
}
