"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Badge, Button, ConfirmDialog, Field } from "@/components/primitives";
import { GroupCapabilityEditor } from "@/components/authorization/CapabilityEditor";
import { ButtonTabs, Modal, Notice, SearchInput, Table, Td, Th, Tr } from "@/components/ui";
import { useAdminUsers } from "@/hooks/useAdminAnalytics";
import {
  useAddGroupMembers,
  useDeleteGroup,
  useGroupMembers,
  useRemoveGroupMember,
  useUpdateGroup,
} from "@/hooks/useGroups";
import { useErrorMessage } from "@/lib/errors";
import type { Group } from "@/lib/api";
import { formatDateTime } from "./format";
import { Pagination } from "./Pagination";
import { RoleBadge } from "./UserBadges";

type Tab = "general" | "permissions" | "members";
const MEMBERS_PAGE = 25;

export function isKeycloakGroup(group: Group): boolean {
  return Boolean(group.lastSyncedAt || group.keycloakId);
}

/** Edit one group: identity, capabilities and members. */
export function GroupDialog({
  group,
  canWrite,
  onClose,
}: {
  group: Group | null;
  canWrite: boolean;
  onClose: () => void;
}) {
  const t = useTranslations("people.groups.dialog");
  const [tab, setTab] = useState<Tab>("general");
  return (
    <Modal
      open={group !== null}
      title={group ? t("title", { group: group.name }) : ""}
      onClose={() => {
        setTab("general");
        onClose();
      }}
      size="lg"
      header={
        <ButtonTabs<Tab>
          label={t("tabsLabel")}
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "general", label: t("tabs.general") },
            { value: "permissions", label: t("tabs.permissions") },
            { value: "members", label: t("tabs.members") },
          ]}
        />
      }
    >
      {group && tab === "general" ? (
        <GeneralTab key={group.id} group={group} canWrite={canWrite} onDeleted={onClose} />
      ) : null}
      {group && tab === "permissions" ? (
        <GroupCapabilityEditor groupId={group.id} groupLabel={group.name} />
      ) : null}
      {group && tab === "members" ? <MembersTab group={group} canWrite={canWrite} /> : null}
    </Modal>
  );
}

function GeneralTab({ group, canWrite, onDeleted }: { group: Group; canWrite: boolean; onDeleted: () => void }) {
  const t = useTranslations("people.groups.dialog");
  const locale = useLocale();
  const errorMessage = useErrorMessage();
  const update = useUpdateGroup();
  const remove = useDeleteGroup();
  const [name, setName] = useState(group.name);
  const [confirming, setConfirming] = useState(false);
  const managed = isKeycloakGroup(group);
  const changed = name.trim() !== group.name && name.trim() !== "";

  return (
    <div className="space-y-6">
      {managed ? (
        <Notice tone="info" title={t("keycloakTitle")}>
          {group.lastSyncedAt
            ? t("keycloakSynced", { date: formatDateTime(group.lastSyncedAt, locale) })
            : t("keycloakLinked")}
        </Notice>
      ) : null}

      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (changed) update.mutate({ id: group.id, data: { name: name.trim() } });
        }}
      >
        <Field
          label={t("name")}
          value={name}
          onChange={(event) => setName(event.target.value)}
          disabled={!canWrite || Boolean(group.lastSyncedAt)}
          required
        />
        {group.lastSyncedAt ? <p className="text-xs text-aurum-text-3">{t("renameBlocked")}</p> : null}
        {update.isError ? <Notice tone="error">{errorMessage(update.error)}</Notice> : null}
        {update.isSuccess && !changed ? <Notice tone="success">{t("saved")}</Notice> : null}
        {canWrite && !group.lastSyncedAt ? (
          <div className="flex justify-end">
            <Button type="submit" disabled={!changed || update.isPending}>
              {update.isPending ? t("saving") : t("save")}
            </Button>
          </div>
        ) : null}
      </form>

      {canWrite ? (
        <section className="rounded-input border border-aurum-err-fg/30 p-4">
          <h3 className="text-sm font-semibold text-aurum-text">{t("deleteTitle")}</h3>
          <p className="mt-1 text-xs leading-5 text-aurum-text-3">
            {managed ? t("deleteManaged") : t("deleteHint")}
          </p>
          <Button
            variant="danger"
            className="mt-3"
            disabled={managed || remove.isPending}
            onClick={() => setConfirming(true)}
          >
            {t("delete")}
          </Button>
        </section>
      ) : null}

      <ConfirmDialog
        open={confirming}
        title={t("deleteConfirmTitle")}
        description={remove.isError ? errorMessage(remove.error) : t("deleteConfirm", { group: group.name })}
        confirmLabel={t("delete")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() =>
          remove.mutate(group.id, {
            onSuccess: () => {
              setConfirming(false);
              onDeleted();
            },
          })
        }
      />
    </div>
  );
}

function MembersTab({ group, canWrite }: { group: Group; canWrite: boolean }) {
  const t = useTranslations("people.groups.dialog");
  const errorMessage = useErrorMessage();
  const [offset, setOffset] = useState(0);
  const [adding, setAdding] = useState(false);
  const members = useGroupMembers(group.id, { limit: MEMBERS_PAGE, offset });
  const remove = useRemoveGroupMember();
  const rows = members.data?.data ?? [];

  return (
    <div className="space-y-4">
      <Notice>{t("membersHint", { group: group.name })}</Notice>
      {canWrite ? (
        <div className="flex justify-end">
          <Button variant="secondary" onClick={() => setAdding((open) => !open)} aria-expanded={adding}>
            {adding ? t("addMembersClose") : t("addMembers")}
          </Button>
        </div>
      ) : null}
      {adding ? <AddMembers group={group} /> : null}
      {members.error ? <Notice tone="error">{errorMessage(members.error)}</Notice> : null}
      {remove.isError ? <Notice tone="error">{errorMessage(remove.error)}</Notice> : null}
      {members.isLoading ? (
        <p role="status" className="text-sm text-aurum-text-3">{t("membersLoading")}</p>
      ) : rows.length === 0 && !members.error ? (
        <p className="text-sm text-aurum-text-3">{t("membersEmpty")}</p>
      ) : rows.length > 0 ? (
        <Table label={t("tabs.members")}>
          <thead>
            <tr>
              <Th>{t("memberName")}</Th>
              <Th>{t("memberRole")}</Th>
              <Th>{t("memberSource")}</Th>
              {canWrite ? <Th className="text-right">{t("memberActions")}</Th> : null}
            </tr>
          </thead>
          <tbody>
            {rows.map((member) => (
              <Tr key={member.id}>
                <Td>
                  <p className="font-medium text-aurum-text">{member.fullName || member.email}</p>
                  {member.fullName ? <p className="text-xs text-aurum-text-3">{member.email}</p> : null}
                </Td>
                <Td><RoleBadge role={member.role} /></Td>
                <Td>
                  {member.source === "manual" ? (
                    <Badge color="amber">{t("sourceManual")}</Badge>
                  ) : (
                    <Badge color="blue">Keycloak</Badge>
                  )}
                </Td>
                {canWrite ? (
                  <Td className="text-right">
                    {member.source === "manual" ? (
                      <button
                        type="button"
                        disabled={remove.isPending}
                        onClick={() => remove.mutate({ groupId: group.id, userId: member.id })}
                        aria-label={t("removeAria", { user: member.fullName || member.email })}
                        className="rounded-btn px-2 py-1.5 text-xs font-medium text-aurum-err-fg hover:bg-aurum-surface-3 disabled:opacity-50"
                      >
                        {t("remove")}
                      </button>
                    ) : (
                      <span className="text-xs text-aurum-text-3">{t("managedInKeycloak")}</span>
                    )}
                  </Td>
                ) : null}
              </Tr>
            ))}
          </tbody>
        </Table>
      ) : null}
      <Pagination
        offset={offset}
        pageSize={MEMBERS_PAGE}
        shown={rows.length}
        total={members.data?.pagination?.total ?? undefined}
        hasMore={members.data?.pagination?.hasMore ?? false}
        onChange={setOffset}
      />
    </div>
  );
}

/** Find people in the organization and add them to the group by hand. */
function AddMembers({ group }: { group: Group }) {
  const t = useTranslations("people.groups.dialog");
  const errorMessage = useErrorMessage();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const found = useAdminUsers({ limit: 10, offset: 0, search: search.trim() });
  const inGroup = useGroupMembers(group.id, { limit: 100, offset: 0 });
  const memberIds = new Set((inGroup.data?.data ?? []).map((member) => member.id));
  const add = useAddGroupMembers();
  const candidates = found.data?.data ?? [];

  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  return (
    <section className="space-y-3 rounded-input border border-aurum-border p-4" aria-label={t("addMembers")}>
      <SearchInput value={search} onChange={setSearch} placeholder={t("searchPeople")} className="max-w-none" />
      {found.error ? <Notice tone="error">{errorMessage(found.error)}</Notice> : null}
      {found.isLoading ? (
        <p role="status" className="text-sm text-aurum-text-3">{t("membersLoading")}</p>
      ) : candidates.length === 0 && !found.error ? (
        <p className="text-sm text-aurum-text-3">{t("noPeople")}</p>
      ) : (
        <ul className="divide-y divide-aurum-border">
          {candidates.map((person) => {
            const already = memberIds.has(person.id);
            return (
              <li key={person.id}>
                <label className="flex items-center gap-3 py-2 text-sm">
                  <input
                    type="checkbox"
                    checked={already || selected.has(person.id)}
                    disabled={already}
                    onChange={() => toggle(person.id)}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium text-aurum-text">{person.fullName || person.email}</span>
                    {person.fullName ? <span className="block text-xs text-aurum-text-3">{person.email}</span> : null}
                  </span>
                  {already ? <span className="text-xs text-aurum-text-3">{t("alreadyMember")}</span> : null}
                </label>
              </li>
            );
          })}
        </ul>
      )}
      {add.isError ? <Notice tone="error">{errorMessage(add.error)}</Notice> : null}
      <div className="flex justify-end">
        <Button
          disabled={selected.size === 0 || add.isPending}
          onClick={() =>
            add.mutate(
              { groupId: group.id, userIds: [...selected] },
              { onSuccess: () => setSelected(new Set()) },
            )
          }
        >
          {add.isPending ? t("adding") : t("addSelected", { count: selected.size })}
        </Button>
      </div>
    </section>
  );
}
