"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/primitives/Button";
import {
  useCapabilityCatalog,
  useGroupCapabilities,
  useOrganizationCapabilities,
  usePutGroupCapabilities,
  usePutOrganizationCapabilities,
} from "@/hooks/useAuthorization";
import { useErrorMessage } from "@/lib/errors";
import type { CapabilityDefinition } from "@/lib/api";
import { useTranslations } from "next-intl";

interface CapabilityEditorViewProps {
  title: string;
  saveLabel: string;
  catalog: CapabilityDefinition[];
  assigned: string[];
  isLoading: boolean;
  error: unknown;
  isPending: boolean;
  mutationError: unknown;
  onSave: (capabilities: string[], onSuccess: () => void) => void;
}

function CapabilityEditorView({
  title,
  saveLabel,
  catalog,
  assigned,
  isLoading,
  error,
  isPending,
  mutationError,
  onSave,
}: CapabilityEditorViewProps) {
  const t = useTranslations("authorization.capabilities");
  const errorMessage = useErrorMessage();
  const assignedKey = assigned.join("\u0000");
  const [desired, setDesired] = useState<string[]>(assigned);
  const [savedSignature, setSavedSignature] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- query data seeds editable draft
    setDesired(assignedKey ? assignedKey.split("\u0000") : []);
  }, [assignedKey]);

  const toggle = (key: string) => {
    setSavedSignature(null);
    setDesired((current) =>
      current.includes(key) ? current.filter((item) => item !== key) : [...current, key]
    );
  };

  const orderedDesired = catalog
    .map((capability) => capability.key)
    .filter((key) => desired.includes(key));
  const assignedSignature = `${title}\u0000${assignedKey}`;

  return (
    <section className="rounded-md border border-aurum-border bg-aurum-surface p-4">
      <h3 className="font-medium text-aurum-text">{title}</h3>
      {isLoading ? <p className="mt-2 text-sm text-aurum-text-3">{t("loading")}</p> : null}
      {error ? (
        <p className="mt-2 text-sm text-aurum-err-fg">{errorMessage(error)}</p>
      ) : null}
      <div className="mt-3 space-y-2">
        {catalog.map((capability) => (
          <label
            key={capability.key}
            className="flex items-start gap-3 rounded bg-aurum-surface-2 p-3 text-sm"
          >
            <input
              type="checkbox"
              checked={desired.includes(capability.key)}
              onChange={() => toggle(capability.key)}
              className="mt-1"
            />
            <span>
              <span className="block font-medium text-aurum-text">{capability.label}</span>
              <span className="block text-xs text-aurum-text-3">{capability.description}</span>
              <span className="block font-mono text-xs text-aurum-text-3">{capability.key}</span>
            </span>
          </label>
        ))}
      </div>
      {mutationError ? (
        <p className="mt-3 text-sm text-aurum-err-fg">{errorMessage(mutationError)}</p>
      ) : null}
      {savedSignature === assignedSignature ? (
        <p className="mt-3 text-sm text-aurum-ok-fg">{t("saved")}</p>
      ) : null}
      <div className="mt-4">
        <Button
          type="button"
          disabled={isPending || isLoading}
          onClick={() =>
            onSave(orderedDesired, () =>
              setSavedSignature(`${title}\u0000${orderedDesired.join("\u0000")}`)
            )
          }
        >
          {isPending ? t("saving") : saveLabel}
        </Button>
      </div>
    </section>
  );
}

export function OrganizationCapabilityEditor() {
  const t = useTranslations("authorization.capabilities");
  const catalog = useCapabilityCatalog();
  const assignment = useOrganizationCapabilities();
  const update = usePutOrganizationCapabilities();

  return (
    <CapabilityEditorView
      title={t("organizationTitle")}
      saveLabel={t("organizationSave")}
      catalog={catalog.data?.data ?? []}
      assigned={assignment.data?.data.capabilities ?? []}
      isLoading={catalog.isLoading || assignment.isLoading}
      error={catalog.error ?? assignment.error}
      isPending={update.isPending}
      mutationError={update.isError ? update.error : null}
      onSave={(capabilities, onSuccess) => update.mutate(capabilities, { onSuccess })}
    />
  );
}

export interface GroupCapabilityEditorProps {
  groupId: string;
  groupLabel: string;
}

export function GroupCapabilityEditor({ groupId, groupLabel }: GroupCapabilityEditorProps) {
  const t = useTranslations("authorization.capabilities");
  const catalog = useCapabilityCatalog();
  const assignment = useGroupCapabilities(groupId);
  const update = usePutGroupCapabilities();

  return (
    <CapabilityEditorView
      title={t("groupTitle", { group: groupLabel })}
      saveLabel={t("groupSave", { group: groupLabel })}
      catalog={catalog.data?.data ?? []}
      assigned={assignment.data?.data.capabilities ?? []}
      isLoading={catalog.isLoading || assignment.isLoading}
      error={catalog.error ?? assignment.error}
      isPending={update.isPending}
      mutationError={update.isError ? update.error : null}
      onSave={(capabilities, onSuccess) =>
        update.mutate({ groupId, capabilities }, { onSuccess })
      }
    />
  );
}
