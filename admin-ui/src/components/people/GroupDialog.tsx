"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button, ConfirmDialog, Field } from "@/components/primitives";
import { GroupCapabilityEditor } from "@/components/authorization/CapabilityEditor";
import { ButtonTabs, Modal, Notice, Table, Td, Th, Tr } from "@/components/ui";
import { useAdminUsers } from "@/hooks/useAdminAnalytics";
import { useDeleteGroup, useUpdateGroup } from "@/hooks/useGroups";
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

/** Edit one group: identity, capabilities and (read-only) members. */
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
      {group && tab === "members" ? <MembersTab group={group} /> : null}
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

function MembersTab({ group }: { group: Group }) {
  const t = useTranslations("people.groups.dialog");
  const errorMessage = useErrorMessage();
  const [offset, setOffset] = useState(0);
  const members = useAdminUsers({ limit: MEMBERS_PAGE, offset, groupId: group.id });
  const rows = members.data?.data ?? [];

  return (
    <div className="space-y-4">
      <Notice>{t("membersHint", { group: group.name })}</Notice>
      {members.error ? <Notice tone="error">{errorMessage(members.error)}</Notice> : null}
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
            </tr>
          </thead>
          <tbody>
            {rows.map((user) => (
              <Tr key={user.id}>
                <Td>
                  <p className="font-medium text-aurum-text">{user.fullName || user.email}</p>
                  {user.fullName ? <p className="text-xs text-aurum-text-3">{user.email}</p> : null}
                </Td>
                <Td><RoleBadge role={user.role} /></Td>
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
