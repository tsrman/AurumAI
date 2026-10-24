"use client";

import { useMemo, useState } from "react";
import { BookOpen, ExternalLink, KeyRound } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { AccessDialog, type AccessTarget } from "@/components/authorization/AccessDialog";
import { AdminGate, EmptyState, Notice, PageBody, PageHeader, RowAction, RowActions, SearchInput, Table, Td, Th, Tr } from "@/components/ui";
import { formatDate } from "@/components/people/format";
import { useKnowledgeBases } from "@/hooks/useKnowledgeBases";
import { useErrorMessage } from "@/lib/errors";
import { getRuntimeConfig } from "@/lib/runtime-config";

export default function AdminKnowledgePage() {
  return <AdminGate>{({ canWrite }) => <KnowledgeContent canWrite={canWrite} />}</AdminGate>;
}

function KnowledgeContent({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("knowledgeAdmin");
  const locale = useLocale();
  const errorMessage = useErrorMessage();
  const bases = useKnowledgeBases();
  const [search, setSearch] = useState("");
  const [access, setAccess] = useState<AccessTarget | null>(null);
  const appUrl = getRuntimeConfig().appUrl;
  const rows = useMemo(() => bases.data?.data ?? [], [bases.data]);
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return query
      ? rows.filter((base) => `${base.name}\n${base.description ?? ""}`.toLocaleLowerCase().includes(query))
      : rows;
  }, [rows, search]);

  const openInChat = (
    <a
      href={`${appUrl}/knowledge`}
      className="inline-flex items-center gap-2 rounded-btn border border-aurum-border bg-aurum-surface px-4 py-2 text-sm font-semibold text-aurum-text-2 hover:bg-aurum-surface-3"
    >
      <ExternalLink className="h-4 w-4" aria-hidden="true" />
      {t("manageInChat")}
    </a>
  );

  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} actions={openInChat} />
      <PageBody>
        <SearchInput value={search} onChange={setSearch} placeholder={t("search")} />
        {bases.error ? <Notice tone="error">{errorMessage(bases.error)}</Notice> : null}
        {bases.isLoading ? (
          <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
        ) : filtered.length === 0 && !bases.error ? (
          <EmptyState
            icon={<BookOpen className="h-8 w-8" aria-hidden="true" />}
            title={rows.length ? t("noMatches") : t("empty")}
            description={rows.length ? undefined : t("emptyHint")}
          />
        ) : filtered.length > 0 ? (
          <Table label={t("title")}>
            <thead>
              <tr>
                <Th>{t("columns.name")}</Th>
                <Th>{t("columns.updated")}</Th>
                <Th className="text-right">{t("columns.actions")}</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((base) => (
                <Tr key={base.id}>
                  <Td>
                    <div className="flex items-center gap-3">
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn bg-c-amber-bg text-c-amber-fg">
                        <BookOpen className="h-4 w-4" aria-hidden="true" />
                      </span>
                      <div className="min-w-0">
                        <p className="truncate font-medium text-aurum-text">{base.name}</p>
                        {base.description ? <p className="line-clamp-1 text-xs text-aurum-text-3">{base.description}</p> : null}
                      </div>
                    </div>
                  </Td>
                  <Td className="whitespace-nowrap">{formatDate(base.updatedAt, locale)}</Td>
                  <Td>
                    <RowActions>
                      {canWrite ? (
                        <RowAction
                          icon={<KeyRound className="h-4 w-4" aria-hidden="true" />}
                          label={t("access")}
                          ariaLabel={t("accessAria", { base: base.name })}
                          onClick={() => setAccess({ resourceType: "knowledge_base", resourceId: base.id, label: base.name })}
                        />
                      ) : null}
                      <a
                        href={`${appUrl}/knowledge/${base.id}`}
                        aria-label={t("openAria", { base: base.name })}
                        className="inline-flex items-center gap-1.5 rounded-btn px-2 py-1.5 text-xs font-medium text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text"
                      >
                        <ExternalLink className="h-4 w-4" aria-hidden="true" />
                        {t("open")}
                      </a>
                    </RowActions>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </PageBody>
      <AccessDialog target={access} onClose={() => setAccess(null)} />
    </>
  );
}
