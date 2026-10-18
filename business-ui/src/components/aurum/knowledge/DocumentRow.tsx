"use client";

import * as React from "react";
import { FileText, Trash2 } from "lucide-react";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { StatusTag } from "./StatusTag";
import { useErrorMessage } from "@/lib/errors";
import type { DocumentDto } from "@/lib/documents-api";
import { useTranslations } from "next-intl";

export interface DocumentRowProps {
  doc: DocumentDto;
  canDelete: boolean;
  deleting: boolean;
  onDelete: (id: string) => Promise<void>;
}

export function DocumentRow({
  doc,
  canDelete,
  deleting,
  onDelete,
}: DocumentRowProps) {
  const t = useTranslations("knowledge.document");
  const errorMessage = useErrorMessage();
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState<string | null>(null);
  const name = doc.source_filename || doc.title;
  const type = (doc.source_filename?.split(".").pop() || "TXT").toUpperCase();
  const status =
    doc.status === "indexed"
      ? "indexed"
      : doc.status === "failed"
        ? "failed"
        : "processing";

  const handleDelete = async () => {
    setDeleteError(null);
    try {
      await onDelete(doc.id);
      setConfirmOpen(false);
    } catch (error) {
      setDeleteError(errorMessage(error));
    }
  };

  return (
    <>
      <tr className="border-b border-aurum-border-soft last:border-b-0">
        <td scope="row" className="px-4 py-3">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 shrink-0 text-aurum-text-3" aria-hidden="true" />
            <span className="truncate text-sm font-medium text-aurum-text">{name}</span>
          </div>
        </td>
        <td className="px-4 py-3 text-sm text-aurum-text-2">{type}</td>
        <td className="px-4 py-3 text-sm text-aurum-text-2">—</td>
        <td className="px-4 py-3 text-sm text-aurum-text-2">{doc.chunk_count}</td>
        <td className="px-4 py-3">
          <StatusTag status={status} />
          {status === "failed" ? (
            <p className="mt-1 text-xs text-aurum-err-fg">
              {t("processingFailed")}
            </p>
          ) : null}
        </td>
        {canDelete ? (
          <td className="px-4 py-3 text-right">
            <button
              type="button"
              aria-label={t("deleteAria", { name })}
              disabled={deleting}
              className="rounded-btn p-2 text-aurum-err-fg transition hover:bg-aurum-err-bg focus:outline-none focus:ring-2 focus:ring-aurum-err-fg/40 disabled:opacity-50"
              onClick={() => {
                setDeleteError(null);
                setConfirmOpen(true);
              }}
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" />
            </button>
            {deleteError ? (
              <p role="alert" className="mt-1 text-xs text-aurum-err-fg">
                {deleteError}
              </p>
            ) : null}
          </td>
        ) : null}
      </tr>
      <ConfirmDialog
        open={confirmOpen}
        title={t("deleteTitle")}
        description={t("deleteDescription", { name })}
        confirmLabel={t("confirm")}
        destructive
        onCancel={() => setConfirmOpen(false)}
        onConfirm={() => void handleDelete()}
      />
    </>
  );
}
