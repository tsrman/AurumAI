"use client";

import { useEffect, useMemo, useState } from "react";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { Building2, Plus, User, Users, X } from "lucide-react";
import { Button } from "@/components/primitives/Button";
import { IconButton } from "@/components/primitives/IconButton";
import { Notice, controlClass } from "@/components/ui";
import {
  useAuthorizationSubjects,
  useReconcileGrants,
  useResourceGrants,
} from "@/hooks/useAuthorization";
import { useErrorMessage } from "@/lib/errors";
import type {
  AccessPermission,
  AuthorizationPrincipal,
  GrantSpec,
  ResourceType,
} from "@/lib/api";
import { AccessPreview } from "./AccessPreview";
import { useTranslations } from "next-intl";

export interface GrantEditorProps {
  resourceType: ResourceType;
  resourceId: string;
  resourceLabel: string;
}

const PRINCIPAL_ICONS = { organization: Building2, group: Users, user: User } as const;

function principalKey(principal: AuthorizationPrincipal | GrantSpec): string {
  return `${principal.principalType}:${principal.principalId}`;
}

export function GrantEditor({ resourceType, resourceId, resourceLabel }: GrantEditorProps) {
  const t = useTranslations("authorization.grants");
  const errorMessage = useErrorMessage();
  const subjectsQuery = useAuthorizationSubjects();
  const grantsQuery = useResourceGrants(resourceType, resourceId);
  const reconcile = useReconcileGrants();
  const [desired, setDesired] = useState<GrantSpec[]>([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [permission, setPermission] = useState<AccessPermission>("read");
  const [confirming, setConfirming] = useState(false);
  const isLoading = subjectsQuery.isLoading || grantsQuery.isLoading;
  // A background refetch reseeds the draft; edits made before it lands would be lost.
  const isBusy = isLoading || grantsQuery.isFetching;
  // Every response carries fresh meta, so seed from the structurally shared rows only.
  const serverGrants = grantsQuery.data?.data;

  const subjects = useMemo(
    () => subjectsQuery.data?.data ?? [],
    [subjectsQuery.data]
  );
  const subjectByKey = useMemo(
    () => new Map(subjects.map((subject) => [principalKey(subject), subject])),
    [subjects]
  );

  useEffect(() => {
    if (serverGrants) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- server state seeds an editable draft
      setDesired(
        serverGrants.map(({ principalType, principalId, permission: access }) => ({
          principalType,
          principalId,
          permission: access,
        }))
      );
    }
  }, [serverGrants]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- first loaded subject is the form default
    if (!selectedKey && subjects.length > 0) setSelectedKey(principalKey(subjects[0]));
  }, [selectedKey, subjects]);

  const selectedSubject = subjectByKey.get(selectedKey) ?? null;

  const addGrant = () => {
    if (!selectedSubject) return;
    const next: GrantSpec = {
      principalType: selectedSubject.principalType,
      principalId: selectedSubject.principalId,
      permission,
    };
    setDesired((current) => [
      ...current.filter((item) => principalKey(item) !== selectedKey),
      next,
    ]);
  };

  const confirmSave = () => {
    reconcile.mutate(
      { resourceType, resourceId, grants: desired },
      { onSuccess: () => setConfirming(false) }
    );
  };

  return (
    <div className="space-y-5">
      {subjectsQuery.error || grantsQuery.error ? (
        <Notice tone="error">{errorMessage(subjectsQuery.error ?? grantsQuery.error)}</Notice>
      ) : null}

      {isLoading ? (
        <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
      ) : null}

      <section aria-label={t("current")}>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-aurum-text-3">
          {t("current")}
        </h3>
        {desired.length === 0 && !isLoading ? (
          <p className="rounded-input border border-dashed border-aurum-border px-3 py-3 text-sm text-aurum-text-3">
            {t("none")}
          </p>
        ) : (
          <ul className="divide-y divide-aurum-border-soft rounded-input border border-aurum-border">
            {desired.map((grant) => {
              const key = principalKey(grant);
              const subject = subjectByKey.get(key);
              const label = subject?.label ?? grant.principalId;
              const Icon = PRINCIPAL_ICONS[grant.principalType];
              return (
                <li key={key} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <Icon className="h-4 w-4 shrink-0 text-aurum-text-3" aria-hidden="true" />
                  <div className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-aurum-text">{label}</span>
                    <span className="block text-xs text-aurum-text-3">{t(`principal.${grant.principalType}`)}</span>
                  </div>
                  <select
                    aria-label={t("permissionAria", { subject: label })}
                    value={grant.permission}
                    disabled={isBusy}
                    onChange={(event) => {
                      const next = event.target.value as AccessPermission;
                      setDesired((current) =>
                        current.map((item) => (principalKey(item) === key ? { ...item, permission: next } : item))
                      );
                    }}
                    className="rounded-input border border-aurum-border bg-aurum-surface px-2 py-1 text-xs text-aurum-text-2"
                  >
                    <option value="read">{t("permission.read")}</option>
                    <option value="write">{t("permission.write")}</option>
                  </select>
                  <IconButton
                    bare
                    disabled={isBusy}
                    onClick={() => setDesired((current) => current.filter((item) => principalKey(item) !== key))}
                    aria-label={t("removeAria", { subject: label })}
                    className="h-8 w-8 text-aurum-text-3 hover:text-aurum-err-fg disabled:opacity-50"
                  >
                    <X className="h-4 w-4" aria-hidden="true" />
                  </IconButton>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-label={t("addTitle")} className="rounded-input bg-aurum-surface-2 p-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-aurum-text-3">{t("addTitle")}</h3>
        <div className="grid gap-2 sm:grid-cols-[1fr_13rem_auto]">
          <select
            aria-label={t("recipientAria")}
            value={selectedKey}
            onChange={(event) => setSelectedKey(event.target.value)}
            className={controlClass}
          >
            {subjects.map((subject) => (
              <option key={principalKey(subject)} value={principalKey(subject)}>
                {t(`principal.${subject.principalType}`)}: {subject.label}
              </option>
            ))}
          </select>
          <select
            aria-label={t("accessAria")}
            value={permission}
            onChange={(event) => setPermission(event.target.value as AccessPermission)}
            className={controlClass}
          >
            <option value="read">{t("permission.read")}</option>
            <option value="write">{t("permission.write")}</option>
          </select>
          <Button type="button" variant="secondary" onClick={addGrant} disabled={isBusy || !selectedSubject}>
            <Plus className="h-4 w-4" aria-hidden="true" />
            {t("add")}
          </Button>
        </div>
        <p className="mt-2 text-xs leading-5 text-aurum-text-3">{t("hint")}</p>
      </section>

      {reconcile.isError ? <Notice tone="error">{errorMessage(reconcile.error)}</Notice> : null}

      <div className="flex justify-end">
        <Button type="button" disabled={isBusy || reconcile.isPending} onClick={() => setConfirming(true)}>
          {reconcile.isPending ? t("saving") : t("save")}
        </Button>
      </div>

      <AccessPreview
        subject={selectedSubject}
        resources={[{ resourceType, resourceId, label: resourceLabel }]}
      />

      <ConfirmDialog
        open={confirming}
        title={t("applyTitle")}
        description={t("applyDescription", { resource: resourceLabel })}
        confirmLabel={t("apply")}
        onConfirm={confirmSave}
        onCancel={() => setConfirming(false)}
      />
    </div>
  );
}
