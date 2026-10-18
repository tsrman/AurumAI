"use client";

import * as React from "react";
import { Button } from "@/components/primitives/Button";
import { Field } from "@/components/primitives/Field";
import { useKnowledgeSearch } from "@/hooks/useKnowledgeSearch";
import { useErrorMessage } from "@/lib/errors";
import type { DocumentDto } from "@/lib/documents-api";
import type { KnowledgeSearchResponse } from "@/lib/knowledge-api";
import { useTranslations } from "next-intl";

export interface KnowledgeSearchPanelProps {
  knowledgeBaseId: string;
  documents: DocumentDto[];
}

export function KnowledgeSearchPanel({
  knowledgeBaseId,
  documents,
}: KnowledgeSearchPanelProps) {
  const t = useTranslations("knowledge.search");
  const errorMessage = useErrorMessage();
  const [query, setQuery] = React.useState("");
  const [response, setResponse] = React.useState<KnowledgeSearchResponse | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const searchMutation = useKnowledgeSearch(knowledgeBaseId);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) return;
    setError(null);
    try {
      setResponse(await searchMutation.search(trimmed));
    } catch (searchError) {
      setResponse(null);
      setError(errorMessage(searchError));
    }
  };

  const sourceName = (documentId?: string | null, title?: string | null) =>
    documents.find((document) => document.id === documentId)?.source_filename ||
    title ||
    t("fallbackTitle");

  return (
    <section className="rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-card">
      <h2 className="text-sm font-semibold text-aurum-text">{t("title")}</h2>
      <form className="mt-3 flex items-end gap-3" onSubmit={submit}>
        <Field
          label={t("title")}
          className="flex-1"
          value={query}
          disabled={searchMutation.isPending}
          onChange={(event) => setQuery(event.target.value)}
        />
        <Button type="submit" disabled={searchMutation.isPending || !query.trim()}>
          {searchMutation.isPending ? t("searching") : t("submit")}
        </Button>
      </form>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-aurum-err-fg">
          {error}
        </p>
      ) : null}

      {response?.warnings.map((warning) => (
        <p key={warning} role="status" className="mt-3 text-sm text-aurum-text-2">
          {warning}
        </p>
      ))}

      {response && response.results.length === 0 ? (
        <p className="mt-4 text-sm text-aurum-text-3">{t("empty")}</p>
      ) : null}

      {response && response.results.length > 0 ? (
        <ul className="mt-4 space-y-3" aria-label={t("results")}>
          {response.results.map((result, index) => (
            <li
              key={`${result.metadata.document_id ?? "document"}-${index}`}
              className="rounded-input border border-aurum-border-soft bg-aurum-surface-2 p-4"
            >
              <p className="whitespace-pre-wrap text-sm text-aurum-text">{result.content}</p>
              <div className="mt-2 flex flex-wrap gap-3 text-xs text-aurum-text-3">
                <span>
                  {sourceName(
                    result.metadata.document_id,
                    result.metadata.document_title
                  )}
                </span>
                {typeof result.relevance_score === "number" &&
                Number.isFinite(result.relevance_score) ? (
                  <span>{t("relevance", { score: result.relevance_score.toFixed(3) })}</span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
