"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, FileText, HardDrive } from "lucide-react";
import { Badge } from "@/components/primitives/Badge";
import { DocumentTable } from "@/components/aurum/knowledge/DocumentTable";
import { DragZone } from "@/components/aurum/knowledge/DragZone";
import { KnowledgeSearchPanel } from "@/components/aurum/knowledge/KnowledgeSearchPanel";
import { DeleteKnowledgeBaseButton } from "@/components/aurum/knowledge/DeleteKnowledgeBaseButton";
import { StatusTag } from "@/components/aurum/knowledge/StatusTag";
import {
  useDeleteDocument,
  useDocuments,
  useUploadDocument,
} from "@/hooks/useDocuments";
import { useKnowledge } from "@/hooks/useKnowledge";
import { cn } from "@/lib/utils";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

export interface KnowledgeDetailPageProps {
  params: Promise<{ id: string }>;
}

export default function KnowledgeDetailPage({ params }: KnowledgeDetailPageProps) {
  const t = useTranslations("knowledge.detail");
  const errorMessage = useErrorMessage();
  const router = useRouter();
  const resolvedParams = React.use(params);
  const { knowledgeBases, isLoading: knowledgeLoading } = useKnowledge();
  const base = knowledgeBases.find((kb) => kb.id === resolvedParams.id);
  const { data: documents, isLoading: documentsLoading } = useDocuments(
    resolvedParams.id
  );
  const upload = useUploadDocument(resolvedParams.id);
  const removeDocument = useDeleteDocument(resolvedParams.id);
  const [uploadError, setUploadError] = React.useState<string | null>(null);
  const [deletingId, setDeletingId] = React.useState<string | null>(null);

  const handleFiles = React.useCallback(
    async (files: File[]) => {
      setUploadError(null);
      for (const file of files) {
        try {
          await upload.mutateAsync(file);
        } catch (error) {
          setUploadError(
            t("uploadFailed", {
              file: file.name,
              reason: error instanceof Error ? errorMessage(error) : t("unknownError"),
            })
          );
          break;
        }
      }
    },
    [upload, t, errorMessage]
  );

  const handleDeleteDocument = React.useCallback(
    async (id: string) => {
      setDeletingId(id);
      try {
        await removeDocument.mutateAsync(id);
      } finally {
        setDeletingId(null);
      }
    },
    [removeDocument]
  );

  if (knowledgeLoading) {
    return <p className="p-6 text-sm text-aurum-text-3">{t("loading")}</p>;
  }

  if (!base) {
    return (
      <div className="flex h-full flex-1 flex-col items-center justify-center px-6 py-12 text-aurum-text-2">
        <p>{t("notFound")}</p>
        <Link
          href="/knowledge"
          className="mt-4 text-sm font-medium text-aurum-accent hover:underline"
        >
          {t("backArrow")}
        </Link>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-1 flex-col">
      <header className="border-b border-aurum-border-soft bg-aurum-surface px-6 py-4">
        <Link
          href="/knowledge"
          className={cn(
            "mb-2 inline-flex items-center gap-1.5 text-sm text-aurum-text-3 transition hover:text-aurum-text"
          )}
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          {t("back")}
        </Link>

        <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <Badge color={base.badgeColor}>{base.badge}</Badge>
            <h1 className="text-base font-semibold text-aurum-text">{base.name}</h1>
          </div>
          <div className="flex flex-col items-end gap-2">
            <StatusTag status={base.status} />
            {base.canWrite ? (
              <div className="text-right">
                <DeleteKnowledgeBaseButton
                  knowledgeBaseId={base.id}
                  knowledgeBaseName={base.name}
                  disabled={(documents?.length ?? 0) > 0}
                  onDeleted={() => router.push("/knowledge")}
                />
                {(documents?.length ?? 0) > 0 ? (
                  <p className="mt-1 text-xs text-aurum-text-3">
                    {t("deleteDocumentsFirst")}
                  </p>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>

        <p className="mt-2 text-sm text-aurum-text-2">{base.description}</p>

        <div className="mt-3 flex items-center gap-4 text-xs text-aurum-text-3">
          <span className="inline-flex items-center gap-1.5">
            <FileText className="h-3.5 w-3.5" aria-hidden="true" />
            {t("docCount", { count: base.docCount })}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <HardDrive className="h-3.5 w-3.5" aria-hidden="true" />
            {base.size}
          </span>
          <span>{t("updated", { date: base.updated })}</span>
        </div>
      </header>

      <main className="flex flex-1 flex-col gap-6 overflow-y-auto px-6 py-6">
        {base.canWrite && (
          <section>
            <h2 className="mb-3 text-sm font-semibold text-aurum-text">{t("uploadHeading")}</h2>
            <DragZone onFiles={handleFiles} disabled={upload.isPending} />
            {uploadError && (
              <p role="alert" className="mt-2 text-xs text-aurum-err-fg">
                {uploadError}
              </p>
            )}
          </section>
        )}

        <KnowledgeSearchPanel
          knowledgeBaseId={resolvedParams.id}
          documents={documents ?? []}
        />

        <section className="flex-1">
          <h2 className="mb-3 text-sm font-semibold text-aurum-text">{t("documents")}</h2>
          {documentsLoading ? (
            <p className="text-sm text-aurum-text-3">{t("loading")}</p>
          ) : (
            <DocumentTable
              docs={documents ?? []}
              canWrite={base.canWrite}
              deletingId={deletingId}
              onDelete={handleDeleteDocument}
            />
          )}
        </section>
      </main>
    </div>
  );
}
