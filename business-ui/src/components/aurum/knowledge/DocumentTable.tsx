"use client";

import { DocumentRow } from "./DocumentRow";
import type { DocumentDto } from "@/lib/documents-api";
import { useTranslations } from "next-intl";

export interface DocumentTableProps {
  docs: DocumentDto[];
  canWrite: boolean;
  deletingId: string | null;
  onDelete: (id: string) => Promise<void>;
}

export function DocumentTable({
  docs,
  canWrite,
  deletingId,
  onDelete,
}: DocumentTableProps) {
  const t = useTranslations("knowledge.table");
  return (
    <div className="overflow-hidden rounded-card border border-aurum-border bg-aurum-surface shadow-card">
      <table className="w-full">
        <thead className="bg-aurum-surface-2">
          <tr>
            <th scope="col" className="px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3">
              {t("name")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3">
              {t("type")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3">
              {t("size")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3">
              {t("chunks")}
            </th>
            <th scope="col" className="px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3">
              {t("status")}
            </th>
            {canWrite ? (
              <th scope="col" className="px-4 py-2.5 text-right text-xs font-semibold text-aurum-text-3">
                {t("actions")}
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {docs.length > 0 ? (
            docs.map((doc) => (
              <DocumentRow
                key={doc.id}
                doc={doc}
                canDelete={canWrite}
                deleting={deletingId === doc.id}
                onDelete={onDelete}
              />
            ))
          ) : (
            <tr>
              <td
                colSpan={canWrite ? 6 : 5}
                className="px-4 py-8 text-center text-sm text-aurum-text-3"
              >
                {t("empty")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
