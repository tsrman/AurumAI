"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Archive, ArchiveRestore, Bot, KeyRound, Pencil, Plus, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { Badge, ConfirmDialog, Toggle } from "@/components/primitives";
import { AccessDialog, type AccessTarget } from "@/components/authorization/AccessDialog";
import { AdminGate, EmptyState, Notice, PageBody, PageHeader, RowAction, RowActions, SearchInput, Table, Td, Th, Tr } from "@/components/ui";
import { formatDate } from "@/components/people/format";
import { useAgentDefinitions, useArchiveAgentDefinition, useDeleteAgentDefinition } from "@/hooks/useAgentDefinitions";
import { useErrorMessage } from "@/lib/errors";
import type { AgentDefinition } from "@/lib/api";

const newAgentLink =
  "inline-flex items-center justify-center gap-2 rounded-btn bg-aurum-accent px-4 py-2 font-semibold text-aurum-accent-ink shadow-card transition hover:brightness-105 focus:outline-none focus:ring-2 focus:ring-aurum-accent/50";

export default function AdminAgentsPage() {
  return <AdminGate>{({ canWrite }) => <AgentsContent canWrite={canWrite} />}</AdminGate>;
}

function AgentsContent({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("agentsAdmin.list");
  const locale = useLocale();
  const router = useRouter();
  const errorMessage = useErrorMessage();
  const [showArchived, setShowArchived] = useState(false);
  const agents = useAgentDefinitions(showArchived);
  const remove = useDeleteAgentDefinition();
  const archive = useArchiveAgentDefinition();
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<AgentDefinition | null>(null);
  const [archiving, setArchiving] = useState<AgentDefinition | null>(null);
  const [access, setAccess] = useState<AccessTarget | null>(null);
  const rows = useMemo(() => agents.data?.data ?? [], [agents.data]);
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return query
      ? rows.filter((agent) => `${agent.name}\n${agent.description ?? ""}\n${agent.modelId}`.toLocaleLowerCase().includes(query))
      : rows;
  }, [rows, search]);

  const newAgent = canWrite ? (
    <Link href="/admin/agents/new" className={newAgentLink}>
      <Plus className="h-4 w-4" aria-hidden="true" />
      {t("create")}
    </Link>
  ) : null;

  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} actions={newAgent} />
      <PageBody>
        <div className="flex flex-wrap items-center gap-4">
          <SearchInput value={search} onChange={setSearch} placeholder={t("search")} />
          <label className="flex items-center gap-2 text-sm text-aurum-text-2">
            <Toggle checked={showArchived} onChange={setShowArchived} aria-label={t("showArchived")} />
            {t("showArchived")}
          </label>
        </div>

        {agents.error ? <Notice tone="error">{errorMessage(agents.error)}</Notice> : null}
        {archive.isError ? <Notice tone="error">{errorMessage(archive.error)}</Notice> : null}

        {agents.isLoading ? (
          <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
        ) : filtered.length === 0 && !agents.error ? (
          <EmptyState
            icon={<Bot className="h-8 w-8" aria-hidden="true" />}
            title={rows.length ? t("noMatches") : t("empty")}
            description={rows.length ? undefined : t("emptyHint")}
            action={rows.length ? undefined : newAgent}
          />
        ) : filtered.length > 0 ? (
          <Table label={t("title")}>
            <thead>
              <tr>
                <Th>{t("columns.name")}</Th>
                <Th>{t("columns.model")}</Th>
                <Th>{t("columns.tools")}</Th>
                <Th>{t("columns.updated")}</Th>
                <Th className="text-right">{t("columns.actions")}</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((agent) => {
                const writable = canWrite && agent.canWrite;
                return (
                  <Tr key={agent.id} onClick={writable && !agent.archivedAt ? () => router.push(`/admin/agents/${agent.id}`) : undefined}>
                    <Td>
                      <div className="flex items-center gap-3">
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn bg-aurum-accent-soft text-aurum-text">
                          <Bot className="h-4 w-4" aria-hidden="true" />
                        </span>
                        <div className="min-w-0">
                          <p className="truncate font-medium text-aurum-text">
                            {agent.name}
                            {agent.kind === "hermes" ? <Badge color="violet" className="ml-2">{t("hermes")}</Badge> : null}
                            {agent.archivedAt ? <Badge color="amber" className="ml-2">{t("archived")}</Badge> : null}
                          </p>
                          {agent.description ? <p className="line-clamp-1 text-xs text-aurum-text-3">{agent.description}</p> : null}
                        </div>
                      </div>
                    </Td>
                    <Td className="font-mono text-xs">{agent.modelId}</Td>
                    <Td className="whitespace-nowrap text-xs">
                      {t("toolsSummary", { skills: agent.skillIds.length, bases: agent.knowledgeBaseIds.length })}
                    </Td>
                    <Td className="whitespace-nowrap">{formatDate(agent.updatedAt, locale)}</Td>
                    <Td>
                      <RowActions>
                        {canWrite ? (
                          <RowAction icon={<KeyRound className="h-4 w-4" aria-hidden="true" />} label={t("access")} ariaLabel={t("accessAria", { agent: agent.name })} onClick={() => setAccess({ resourceType: "agent_definition", resourceId: agent.id, label: agent.name })} />
                        ) : null}
                        {writable ? (
                          <>
                            {agent.archivedAt ? (
                              <RowAction icon={<ArchiveRestore className="h-4 w-4" aria-hidden="true" />} label={t("restore")} ariaLabel={t("restoreAria", { agent: agent.name })} onClick={() => archive.mutate({ id: agent.id, archived: false })} />
                            ) : (
                              <>
                                <RowAction icon={<Pencil className="h-4 w-4" aria-hidden="true" />} label={t("edit")} ariaLabel={t("editAria", { agent: agent.name })} onClick={() => router.push(`/admin/agents/${agent.id}`)} />
                                <RowAction icon={<Archive className="h-4 w-4" aria-hidden="true" />} label={t("archive")} ariaLabel={t("archiveAria", { agent: agent.name })} onClick={() => setArchiving(agent)} />
                              </>
                            )}
                            <RowAction tone="danger" icon={<Trash2 className="h-4 w-4" aria-hidden="true" />} label={t("delete")} ariaLabel={t("deleteAria", { agent: agent.name })} onClick={() => setDeleting(agent)} />
                          </>
                        ) : null}
                      </RowActions>
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        ) : null}
      </PageBody>

      <AccessDialog target={access} onClose={() => setAccess(null)} />
      <ConfirmDialog
        open={archiving !== null}
        title={t("archiveTitle")}
        description={
          archive.isError && archiving
            ? errorMessage(archive.error)
            : archiving
              ? t("archiveDescription", { agent: archiving.name })
              : undefined
        }
        confirmLabel={t("archive")}
        onCancel={() => {
          archive.reset();
          setArchiving(null);
        }}
        onConfirm={() => {
          if (archiving) archive.mutate({ id: archiving.id, archived: true }, { onSuccess: () => setArchiving(null) });
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        title={t("deleteTitle")}
        description={
          remove.isError ? errorMessage(remove.error) : deleting ? t("deleteDescription", { agent: deleting.name }) : undefined
        }
        confirmLabel={t("delete")}
        destructive
        onCancel={() => {
          remove.reset();
          setDeleting(null);
        }}
        onConfirm={() => {
          if (deleting) remove.mutate(deleting.id, { onSuccess: () => setDeleting(null) });
        }}
      />
    </>
  );
}
