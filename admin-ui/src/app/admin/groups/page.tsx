"use client";

import { useState } from "react";
import { useAdminAccess } from "@/hooks/useAdminAccess";
import { useGroups, useCreateGroup, useDeleteGroup } from "@/hooks/useGroups";
import { useErrorMessage } from "@/lib/errors";
import type { Group } from "@/lib/api";
import { ConfirmDialog } from "@/components/primitives";
import {
  GroupCapabilityEditor,
  OrganizationCapabilityEditor,
} from "@/components/authorization/CapabilityEditor";
import { useTranslations } from "next-intl";

export default function AdminGroupsPage() {
  const t = useTranslations("groups");
  const accessT = useTranslations("access");
  const errorMessage = useErrorMessage();
  const { allowed, canWrite, error: accessError, isLoading: checking } = useAdminAccess();
  const { data, isLoading, error } = useGroups();
  const create = useCreateGroup();
  const remove = useDeleteGroup();
  const [name, setName] = useState("");
  const [deletingGroup, setDeletingGroup] = useState<Group | null>(null);
  const [capabilityGroup, setCapabilityGroup] = useState<Group | null>(null);

  if (checking) return <div className="p-6">{accessT("checking")}</div>;

  if (!allowed) {
    return (
      <div className="rounded-md border border-red-200 bg-red-50 p-6 text-red-700">
        {accessError || accessT("adminRequired")}
      </div>
    );
  }

  const groups = data?.data ?? [];

  const handleCreate = (event: React.FormEvent) => {
    event.preventDefault();
    create.mutate({ name }, { onSuccess: () => setName("") });
  };

  const confirmDelete = () => {
    if (!deletingGroup) return;
    remove.mutate(deletingGroup.id, { onSuccess: () => setDeletingGroup(null) });
  };

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold">{t("title")}</h1>

      <div className="mb-8">
        <OrganizationCapabilityEditor />
      </div>

      {error && !isLoading && (
        <div className="mb-6 rounded-md border border-yellow-200 bg-yellow-50 p-4 text-yellow-800">
          {errorMessage(error)}
        </div>
      )}

      {canWrite && (
        <form onSubmit={handleCreate} className="mb-8 space-y-4 rounded-md border p-4">
          <h2 className="font-medium">{t("create")}</h2>
          <div>
            <label htmlFor="group-name" className="mb-1 block text-sm font-medium">
              {t("name")}
            </label>
            <input
              id="group-name"
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="w-full rounded-md border px-3 py-2"
              required
            />
          </div>
          <button
            type="submit"
            disabled={create.isPending}
            className="rounded-md bg-primary-600 px-4 py-2 text-white disabled:opacity-50"
          >
            {create.isPending ? t("creating") : t("create")}
          </button>
          {create.isError && (
            <p className="text-sm text-red-600">{errorMessage(create.error)}</p>
          )}
        </form>
      )}

      {isLoading ? (
        <div>{t("loading")}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border text-left text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="border px-4 py-2">{t("name")}</th>
                <th className="border px-4 py-2">{t("actions")}</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <tr key={group.id}>
                  <td className="border px-4 py-2">
                    <span className="inline-flex items-center gap-2">
                      {group.name}
                      {group.lastSyncedAt && (
                        <span
                          className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700"
                          title={t("syncedFromKeycloak", { date: group.lastSyncedAt })}
                        >
                          Keycloak
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="border px-4 py-2">
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => setCapabilityGroup(group)}
                        aria-label={t("configureCapabilities", { group: group.name })}
                        className="rounded-md bg-blue-600 px-3 py-1 text-white hover:bg-blue-700"
                      >
                        Capabilities
                      </button>
                      <button
                        type="button"
                        onClick={() => setDeletingGroup(group)}
                        disabled={remove.isPending || !canWrite || Boolean(group.lastSyncedAt || group.keycloakId)}
                        className="rounded-md bg-red-600 px-3 py-1 text-white hover:bg-red-700 disabled:opacity-50"
                      >
                        {t("delete")}
                      </button>
                    </div>
                    {group.lastSyncedAt || group.keycloakId ? (
                      <p className="mt-2 text-xs text-gray-600">{t("managedInKeycloak")}</p>
                    ) : null}
                  </td>
                </tr>
              ))}
              {groups.length === 0 && !error && (
                <tr>
                  <td className="border px-4 py-4 text-center text-gray-500" colSpan={2}>
                    {t("empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {capabilityGroup ? (
        <div className="mt-8">
          <GroupCapabilityEditor
            groupId={capabilityGroup.id}
            groupLabel={capabilityGroup.name}
          />
        </div>
      ) : null}

      <ConfirmDialog
        open={deletingGroup != null}
        title={t("deleteTitle")}
        description={
          remove.isError
            ? errorMessage(remove.error)
            : deletingGroup
            ? t("deleteDescription", { group: deletingGroup.name })
            : undefined
        }
        confirmLabel={t("delete")}
        destructive
        onConfirm={confirmDelete}
        onCancel={() => setDeletingGroup(null)}
      />
    </div>
  );
}
