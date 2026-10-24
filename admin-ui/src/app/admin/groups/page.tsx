"use client";

import { useMemo, useState } from "react";
import { ChevronRight, Plus, Settings2, Users } from "lucide-react";
import { useTranslations } from "next-intl";
import { Badge, Button, Field } from "@/components/primitives";
import { OrganizationCapabilityEditor } from "@/components/authorization/CapabilityEditor";
import { AdminGate, EmptyState, Modal, Notice, PageBody, PageHeader, SearchInput, Table, Td, Th, Tr } from "@/components/ui";
import { PeopleTabs } from "@/components/people/PeopleTabs";
import { GroupDialog, isKeycloakGroup } from "@/components/people/GroupDialog";
import { useCreateGroup, useGroups } from "@/hooks/useGroups";
import { useErrorMessage } from "@/lib/errors";

export default function AdminGroupsPage() {
  const t = useTranslations("people.groups");
  const [creating, setCreating] = useState(false);
  return (
    <AdminGate>
      {({ canWrite }) => (
        <>
          <PageHeader
            title={t("title")}
            description={t("subtitle")}
            tabs={<PeopleTabs />}
            actions={
              canWrite ? (
                <Button onClick={() => setCreating(true)}>
                  <Plus className="h-4 w-4" aria-hidden="true" />
                  {t("create")}
                </Button>
              ) : null
            }
          />
          <GroupsContent canWrite={canWrite} creating={creating} onCreatingChange={setCreating} />
        </>
      )}
    </AdminGate>
  );
}

function GroupsContent({
  canWrite,
  creating,
  onCreatingChange,
}: {
  canWrite: boolean;
  creating: boolean;
  onCreatingChange: (open: boolean) => void;
}) {
  const t = useTranslations("people.groups");
  const errorMessage = useErrorMessage();
  const { data, isLoading, error } = useGroups();
  const [search, setSearch] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [defaultsOpen, setDefaultsOpen] = useState(false);
  const groups = useMemo(() => data?.data ?? [], [data]);
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return query ? groups.filter((group) => group.name.toLocaleLowerCase().includes(query)) : groups;
  }, [groups, search]);
  // Looked up by id so the dialog follows renames and refetches.
  const editing = groups.find((group) => group.id === editingId) ?? null;

  return (
    <PageBody>
      <button
        type="button"
        onClick={() => setDefaultsOpen(true)}
        className="flex w-full items-center gap-4 rounded-card border border-aurum-border bg-aurum-surface px-5 py-4 text-left shadow-card transition hover:bg-aurum-surface-2 focus:outline-none focus:ring-2 focus:ring-aurum-accent/50"
      >
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-aurum-accent-soft text-aurum-text">
          <Settings2 className="h-5 w-5" aria-hidden="true" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-aurum-text">{t("defaults.title")}</span>
          <span className="block text-xs leading-5 text-aurum-text-3">{t("defaults.description")}</span>
        </span>
        <ChevronRight className="h-5 w-5 text-aurum-text-3" aria-hidden="true" />
      </button>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <SearchInput value={search} onChange={setSearch} placeholder={t("search")} />
        <span className="text-sm text-aurum-text-3">{t("count", { count: groups.length })}</span>
      </div>

      {error && !isLoading ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

      {isLoading ? (
        <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
      ) : filtered.length === 0 && !error ? (
        <EmptyState
          icon={<Users className="h-8 w-8" aria-hidden="true" />}
          title={search ? t("noMatches") : t("empty")}
          description={search ? undefined : t("emptyHint")}
        />
      ) : filtered.length > 0 ? (
        <Table label={t("title")}>
          <thead>
            <tr>
              <Th>{t("columns.name")}</Th>
              <Th>{t("columns.source")}</Th>
              <Th>{t("columns.members")}</Th>
              <Th className="text-right">{t("columns.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((group) => (
              <Tr key={group.id} onClick={() => setEditingId(group.id)}>
                <Td className="font-medium text-aurum-text">{group.name}</Td>
                <Td>
                  {isKeycloakGroup(group) ? <Badge color="blue">Keycloak</Badge> : <Badge color="amber">{t("local")}</Badge>}
                </Td>
                <Td>{group.memberCount ?? "—"}</Td>
                <Td className="text-right">
                  <button
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      setEditingId(group.id);
                    }}
                    aria-label={t("editAria", { group: group.name })}
                    className="rounded-btn px-2 py-1.5 text-xs font-medium text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text"
                  >
                    {t("edit")}
                  </button>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      ) : null}

      <CreateGroupDialog open={creating} onClose={() => onCreatingChange(false)} />
      <GroupDialog group={editing} canWrite={canWrite} onClose={() => setEditingId(null)} />
      <Modal open={defaultsOpen} title={t("defaults.title")} onClose={() => setDefaultsOpen(false)}>
        <OrganizationCapabilityEditor />
      </Modal>
    </PageBody>
  );
}

function CreateGroupDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useTranslations("people.groups");
  const errorMessage = useErrorMessage();
  const create = useCreateGroup();
  const [name, setName] = useState("");
  const close = () => {
    setName("");
    create.reset();
    onClose();
  };
  return (
    <Modal open={open} title={t("createTitle")} description={t("createHint")} onClose={close} size="md">
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate({ name: name.trim() }, { onSuccess: close });
        }}
      >
        <Field label={t("columns.name")} value={name} onChange={(event) => setName(event.target.value)} required autoFocus />
        {create.isError ? <Notice tone="error">{errorMessage(create.error)}</Notice> : null}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={close}>{t("cancel")}</Button>
          <Button type="submit" disabled={create.isPending || !name.trim()}>
            {create.isPending ? t("creating") : t("create")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
