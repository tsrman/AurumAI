"use client";

import { useEffect, useMemo, useState } from "react";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { Button } from "@/components/primitives/Button";
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
    <section className="rounded-md border border-aurum-border bg-aurum-surface p-4">
      <h3 className="text-sm font-semibold text-aurum-text">Resource grants</h3>
      <p className="mt-1 text-xs text-aurum-text-3">{resourceLabel}</p>

      {subjectsQuery.error || grantsQuery.error ? (
        <p className="mt-3 text-sm text-aurum-err-fg">
          {errorMessage(subjectsQuery.error ?? grantsQuery.error)}
        </p>
      ) : null}

      {isLoading ? (
        <p className="mt-3 text-sm text-aurum-text-3">{t("loading")}</p>
      ) : null}

      <div className="mt-4 grid gap-3 md:grid-cols-[1fr_10rem_auto]">
        <label className="text-sm text-aurum-text-2">
          {t("recipient")}
          <select
            aria-label={t("recipientAria")}
            value={selectedKey}
            onChange={(event) => setSelectedKey(event.target.value)}
            className="mt-1 block w-full rounded-md border border-aurum-border bg-aurum-surface px-3 py-2"
          >
            {subjects.map((subject) => (
              <option key={principalKey(subject)} value={principalKey(subject)}>
                {t(`principal.${subject.principalType}`)}: {subject.label}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm text-aurum-text-2">
          {t("access")}
          <select
            aria-label={t("accessAria")}
            value={permission}
            onChange={(event) => setPermission(event.target.value as AccessPermission)}
            className="mt-1 block w-full rounded-md border border-aurum-border bg-aurum-surface px-3 py-2"
          >
            <option value="read">read</option>
            <option value="write">write</option>
          </select>
        </label>
        <Button
          type="button"
          variant="secondary"
          onClick={addGrant}
          disabled={isBusy || !selectedSubject}
        >
          {t("add")}
        </Button>
      </div>

      <ul className="mt-4 space-y-2">
        {desired.map((grant) => {
          const key = principalKey(grant);
          const subject = subjectByKey.get(key);
          return (
            <li key={key} className="flex items-center justify-between rounded bg-aurum-surface-2 px-3 py-2 text-sm">
              <div>
                <span className="font-medium text-aurum-text">
                  {subject?.label ?? grant.principalId}
                </span>{" "}
                <span className="text-aurum-text-3">· {grant.permission}</span>
              </div>
              <button
                type="button"
                disabled={isBusy}
                onClick={() => setDesired((current) => current.filter((item) => principalKey(item) !== key))}
                className="text-aurum-err-fg disabled:opacity-50"
                aria-label={t("removeAria", { subject: subject?.label ?? grant.principalId })}
              >
                {t("remove")}
              </button>
            </li>
          );
        })}
      </ul>

      {reconcile.isError ? (
        <p className="mt-3 text-sm text-aurum-err-fg">{errorMessage(reconcile.error)}</p>
      ) : null}

      <div className="mt-4 flex justify-end">
        <Button
          type="button"
          disabled={isBusy || reconcile.isPending}
          onClick={() => setConfirming(true)}
        >
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
    </section>
  );
}
