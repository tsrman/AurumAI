"use client";

import { useMemo, useState } from "react";
import { Cpu, KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { Badge, Button, Slider, Toggle } from "@/components/primitives";
import { AccessDialog, type AccessTarget } from "@/components/authorization/AccessDialog";
import {
  AdminGate,
  EmptyState,
  Notice,
  PageBody,
  PageHeader,
  RowAction,
  SearchInput,
  Section,
  SettingRow,
  Table,
  Td,
  Th,
  Tr,
  controlClass,
} from "@/components/ui";
import {
  useAdminModels,
  useOrganizationImageModels,
  useUpdateOrganizationImageModels,
  useUpdateOrganizationModelSettings,
} from "@/hooks/useAdminModels";
import { useErrorMessage } from "@/lib/errors";
import type { AdminModelSettings } from "@/lib/api";

const DEFAULT_TEMPERATURE_IF_ENABLED = 0.7;

export default function AdminModelsPage() {
  const t = useTranslations("models");
  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} />
      <AdminGate>{({ canWrite }) => <ModelsContent canWrite={canWrite} />}</AdminGate>
    </>
  );
}

function ModelsContent({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("models");
  const errorMessage = useErrorMessage();
  const models = useAdminModels();
  const [access, setAccess] = useState<AccessTarget | null>(null);

  if (models.isLoading) return <p role="status" className="p-6 text-sm text-aurum-text-3">{t("loading")}</p>;
  if (models.error || !models.data) {
    return (
      <PageBody>
        <Notice tone="error">{models.error ? errorMessage(models.error) : t("loadFailed")}</Notice>
      </PageBody>
    );
  }
  const settings = models.data.data;
  const openAccess = (id: string) => setAccess({ resourceType: "model", resourceId: id, label: id });

  return (
    <PageBody>
      <GatewayNotice settings={settings} />
      <DefaultsSection settings={settings} canWrite={canWrite} onGrantAccess={openAccess} />
      <ImageModelsSection canWrite={canWrite} />
      <CatalogSection settings={settings} canWrite={canWrite} onAccess={openAccess} />
      <AccessDialog target={access} onClose={() => setAccess(null)} />
    </PageBody>
  );
}

function GatewayNotice({ settings }: { settings: AdminModelSettings }) {
  const t = useTranslations("models");
  if (settings.gatewayStatus === "available") {
    return <Notice tone="success">{t("gateway.available")}</Notice>;
  }
  if (settings.gatewayStatus === "mock") {
    return <Notice tone="info" title={t("gateway.mock")}>{t("gatewayMockHint")}</Notice>;
  }
  return (
    <Notice tone="warning" title={t("gateway.unavailable")}>
      {t("gatewayUnavailableHint")}
    </Notice>
  );
}

function DefaultsSection({
  settings,
  canWrite,
  onGrantAccess,
}: {
  settings: AdminModelSettings;
  canWrite: boolean;
  onGrantAccess: (id: string) => void;
}) {
  const t = useTranslations("models");
  const errorMessage = useErrorMessage();
  const update = useUpdateOrganizationModelSettings();
  const savedDefault = settings.defaultModel;
  const savedValue = savedDefault ?? "";
  const savedTemperature = settings.defaultTemperature;
  // Drafts remember the saved value they were made against, so a refetch
  // that changes the saved value discards a stale draft.
  const [draft, setDraft] = useState<{ baseline: string; value: string } | null>(null);
  const [temperatureDraft, setTemperatureDraft] = useState<{
    baseline: number | null;
    value: number | null;
  } | null>(null);
  const selected = draft?.baseline === savedValue ? draft.value : savedValue;
  const selectedTemperature =
    temperatureDraft?.baseline === savedTemperature ? temperatureDraft.value : savedTemperature;

  const isStale = savedDefault !== null && !settings.models.some((model) => model.id === savedDefault);
  const changed = selected !== savedValue;
  const selectedDefault = selected || null;
  const selectedModel = settings.models.find((model) => model.id === selectedDefault);
  const requiresGrant = Boolean(selectedModel && !selectedModel.organizationReadable);
  const temperatureChanged = selectedTemperature !== savedTemperature;
  const unavailableSelection = settings.gatewayStatus === "unavailable" && selectedDefault !== null;
  const saveDisabled =
    !canWrite || (!changed && !temperatureChanged) || update.isPending || requiresGrant || unavailableSelection;

  return (
    <Section title={t("defaultsTitle")} description={t("grantsHint")}>
      <SettingRow label={t("defaultModel")} description={t("defaultModelHint")} htmlFor="organization-default-model">
        <select
          id="organization-default-model"
          value={selected}
          disabled={!canWrite}
          onChange={(event) => setDraft({ baseline: savedValue, value: event.target.value })}
          className={controlClass}
        >
          <option value="">{t("noDefault")}</option>
          {isStale ? (
            <option value={savedDefault ?? ""} disabled>
              {t("unavailable", { model: savedDefault ?? "" })}
            </option>
          ) : null}
          {settings.models.map((model) => (
            <option key={model.id} value={model.id}>
              {model.organizationReadable ? model.id : t("requiresAccess", { model: model.id })}
            </option>
          ))}
        </select>
        {savedDefault !== null && !settings.defaultAvailable ? (
          <p className="mt-2 text-xs text-c-amber-fg">{t("staleDefault")}</p>
        ) : null}
      </SettingRow>

      {requiresGrant && selectedModel ? (
        <Notice tone="warning" className="mb-4">
          <p>{t("grantAccessHint")}</p>
          <Button variant="secondary" className="mt-2" onClick={() => onGrantAccess(selectedModel.id)}>
            <KeyRound className="h-4 w-4" aria-hidden="true" />
            {t("grantAccess")}
          </Button>
        </Notice>
      ) : null}

      <SettingRow label={t("defaultTemperature")} description={t("temperatureHint")}>
        <div className="flex items-center gap-3">
          <Toggle
            aria-label={t("overrideTemperature")}
            checked={selectedTemperature !== null}
            disabled={!canWrite}
            onChange={(checked) =>
              setTemperatureDraft({
                baseline: savedTemperature,
                value: checked ? DEFAULT_TEMPERATURE_IF_ENABLED : null,
              })
            }
          />
          {selectedTemperature !== null ? (
            <>
              <Slider
                aria-label={t("defaultTemperature")}
                value={selectedTemperature}
                min={0}
                max={2}
                step={0.1}
                onChange={(value) => setTemperatureDraft({ baseline: savedTemperature, value })}
              />
              <span className="w-8 text-right text-sm tabular-nums text-aurum-text-2">
                {selectedTemperature.toFixed(1)}
              </span>
            </>
          ) : (
            <span className="text-xs text-aurum-text-3">{t("temperatureUnset")}</span>
          )}
        </div>
      </SettingRow>

      {update.isError ? <Notice tone="error" className="mt-4">{errorMessage(update.error)}</Notice> : null}

      {canWrite ? (
        <div className="mt-5 flex justify-end">
          <Button
            disabled={saveDisabled}
            onClick={() =>
              update.mutate(
                { defaultModel: selectedDefault, defaultTemperature: selectedTemperature },
                {
                  onSuccess: () => {
                    setDraft(null);
                    setTemperatureDraft(null);
                  },
                }
              )
            }
          >
            {update.isPending ? t("saving") : t("save")}
          </Button>
        </div>
      ) : null}
    </Section>
  );
}

function ImageModelsSection({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("models.images");
  const errorMessage = useErrorMessage();
  const query = useOrganizationImageModels();
  const update = useUpdateOrganizationImageModels();
  const saved = query.data?.data;
  const baseline = `${saved?.generationModel ?? ""}\u0000${saved?.editModel ?? ""}`;
  const [draft, setDraft] = useState<{ baseline: string; generation: string; edit: string } | null>(null);
  const generation = draft?.baseline === baseline ? draft.generation : (saved?.generationModel ?? "");
  const edit = draft?.baseline === baseline ? draft.edit : (saved?.editModel ?? "");
  const change = (next: { generation?: string; edit?: string }) =>
    setDraft({ baseline, generation: next.generation ?? generation, edit: next.edit ?? edit });

  if (query.isLoading) return null;
  if (query.error || !saved) {
    return (
      <Section title={t("title")}>
        <Notice tone="error">{query.error ? errorMessage(query.error) : t("loadFailed")}</Notice>
      </Section>
    );
  }
  const changed = generation !== (saved.generationModel ?? "") || edit !== (saved.editModel ?? "");
  const options = (current: string) => (
    <>
      <option value="">{t("off")}</option>
      {current && !saved.models.includes(current) ? (
        <option value={current} disabled>{t("unavailable", { model: current })}</option>
      ) : null}
      {saved.models.map((model) => (
        <option key={model} value={model}>{model}</option>
      ))}
    </>
  );

  return (
    <Section title={t("title")} description={t("description")}>
      {!saved.enabled ? <Notice tone="warning" className="mb-4">{t("operatorOff")}</Notice> : null}
      <SettingRow label={t("generation")} description={t("generationHint")} htmlFor="image-generation-model">
        <select
          id="image-generation-model"
          value={generation}
          disabled={!canWrite}
          onChange={(event) => change({ generation: event.target.value })}
          className={controlClass}
        >
          {options(generation)}
        </select>
      </SettingRow>
      <SettingRow label={t("edit")} description={t("editHint")} htmlFor="image-edit-model">
        <select
          id="image-edit-model"
          value={edit}
          disabled={!canWrite}
          onChange={(event) => change({ edit: event.target.value })}
          className={controlClass}
        >
          {options(edit)}
        </select>
      </SettingRow>
      <p className="mt-4 text-xs leading-5 text-aurum-text-3">{t("capabilityHint")}</p>
      {update.isError ? <Notice tone="error" className="mt-4">{errorMessage(update.error)}</Notice> : null}
      {canWrite ? (
        <div className="mt-5 flex justify-end">
          <Button
            disabled={!changed || update.isPending}
            onClick={() =>
              update.mutate(
                { generationModel: generation || null, editModel: edit || null },
                { onSuccess: () => setDraft(null) }
              )
            }
          >
            {update.isPending ? t("saving") : t("save")}
          </Button>
        </div>
      ) : null}
    </Section>
  );
}

function CatalogSection({
  settings,
  canWrite,
  onAccess,
}: {
  settings: AdminModelSettings;
  canWrite: boolean;
  onAccess: (id: string) => void;
}) {
  const t = useTranslations("models");
  const [search, setSearch] = useState("");
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return query ? settings.models.filter((model) => model.id.toLocaleLowerCase().includes(query)) : settings.models;
  }, [settings.models, search]);

  return (
    <Section title={t("catalogTitle")} description={t("catalogHint")}>
      <SearchInput value={search} onChange={setSearch} placeholder={t("search")} className="mb-4" />
      {filtered.length === 0 ? (
        <EmptyState icon={<Cpu className="h-8 w-8" aria-hidden="true" />} title={settings.models.length ? t("noMatches") : t("empty")} />
      ) : (
        <Table label={t("catalogTitle")} className="shadow-none">
          <thead>
            <tr>
              <Th>{t("columns.model")}</Th>
              <Th>{t("columns.availability")}</Th>
              <Th className="text-right">{t("columns.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((model) => (
              <Tr key={model.id}>
                <Td>
                  <span className="font-mono text-xs text-aurum-text">{model.id}</span>
                  {model.isDefault ? <Badge color="amber" className="ml-2">{t("defaultBadge")}</Badge> : null}
                </Td>
                <Td>
                  {model.organizationReadable ? (
                    <Badge color="green">{t("orgReadable")}</Badge>
                  ) : (
                    <Badge color="rose">{t("restricted")}</Badge>
                  )}
                </Td>
                <Td className="text-right">
                  {canWrite ? (
                    <RowAction
                      icon={<KeyRound className="h-4 w-4" aria-hidden="true" />}
                      label={t("access")}
                      ariaLabel={t("accessAria", { model: model.id })}
                      onClick={() => onAccess(model.id)}
                    />
                  ) : null}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </Section>
  );
}
