"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/primitives/Button";
import { Toggle } from "@/components/primitives/Toggle";
import { Notice } from "@/components/ui/Notice";
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
  description?: string;
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
  description,
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
  const catalogT = useTranslations("authorization.capabilityCatalog");
  const errorMessage = useErrorMessage();
  // Known capabilities are labelled from the UI catalogs; a key the UI does
  // not know yet falls back to the server's English metadata.
  const describe = (capability: CapabilityDefinition) => {
    const id = capability.key.replaceAll(".", "_");
    return catalogT.has(`${id}.label`)
      ? { label: catalogT(`${id}.label`), description: catalogT(`${id}.description`) }
      : { label: capability.label, description: capability.description };
  };
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
    <section aria-label={title} className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-aurum-text">{title}</h3>
        {description ? <p className="mt-1 text-xs leading-5 text-aurum-text-3">{description}</p> : null}
      </div>
      {isLoading ? <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p> : null}
      {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}
      <ul className="divide-y divide-aurum-border-soft rounded-input border border-aurum-border">
        {catalog.map((capability) => {
          const { label, description: text } = describe(capability);
          return (
          <li key={capability.key} className="flex items-start justify-between gap-4 px-3 py-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-aurum-text">{label}</p>
              <p className="mt-0.5 text-xs leading-5 text-aurum-text-3">{text}</p>
              <code className="mt-1 block text-[11px] text-aurum-text-3">{capability.key}</code>
            </div>
            <Toggle
              aria-label={`${label} (${capability.key})`}
              checked={desired.includes(capability.key)}
              onChange={() => toggle(capability.key)}
              className="mt-0.5 shrink-0"
            />
          </li>
          );
        })}
      </ul>
      {mutationError ? <Notice tone="error">{errorMessage(mutationError)}</Notice> : null}
      {savedSignature === assignedSignature ? <Notice tone="success">{t("saved")}</Notice> : null}
      <div className="flex justify-end">
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
      description={t("organizationDescription")}
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
      description={t("groupDescription")}
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
