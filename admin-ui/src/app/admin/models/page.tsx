"use client";

import Link from "next/link";
import { useState } from "react";

import { useAdminAccess } from "@/hooks/useAdminAccess";
import {
  useAdminModels,
  useUpdateOrganizationModelSettings,
} from "@/hooks/useAdminModels";
import { useErrorMessage } from "@/lib/errors";
import { Slider } from "@/components/primitives/Slider";
import { useTranslations } from "next-intl";

const DEFAULT_TEMPERATURE_IF_ENABLED = 0.7;



export default function AdminModelsPage() {
  const t = useTranslations("models");
  const accessT = useTranslations("access");
  const errorMessage = useErrorMessage();
  const { allowed, canWrite, error: accessError, isLoading: checking } =
    useAdminAccess();
  const models = useAdminModels();
  const update = useUpdateOrganizationModelSettings();
  const savedDefault = models.data?.data.defaultModel ?? null;
  const savedValue = savedDefault ?? "";
  const [draft, setDraft] = useState<{
    baseline: string;
    value: string;
  } | null>(null);
  const selected = draft?.baseline === savedValue ? draft.value : savedValue;
  const savedTemperature = models.data?.data.defaultTemperature ?? null;
  const [temperatureDraft, setTemperatureDraft] = useState<{
    baseline: number | null;
    value: number | null;
  } | null>(null);
  const selectedTemperature =
    temperatureDraft?.baseline === savedTemperature
      ? temperatureDraft.value
      : savedTemperature;

  if (checking) return <div className="p-6">{accessT("checking")}</div>;
  if (!allowed) {
    return (
      <div className="rounded-md border border-red-200 bg-red-50 p-6 text-red-700">
        {accessError || accessT("adminRequired")}
      </div>
    );
  }
  if (models.isLoading) return <div>{t("loading")}</div>;
  if (models.error || !models.data) {
    return (
      <div className="rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
        {models.error ? errorMessage(models.error) : t("loadFailed")}
      </div>
    );
  }

  const settings = models.data.data;
  const isStale =
    savedDefault !== null &&
    !settings.models.some((model) => model.id === savedDefault);
  const changed = selected !== (savedDefault ?? "");
  const selectedDefault = selected || null;
  const temperatureChanged = selectedTemperature !== savedTemperature;
  const unavailableSelection =
    settings.gatewayStatus === "unavailable" && selectedDefault !== null;
  const saveDisabled =
    !canWrite ||
    (!changed && !temperatureChanged) ||
    update.isPending ||
    unavailableSelection;

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t("title")}</h1>
        <p className="mt-2 text-sm text-gray-600">
          {t("subtitle")}
        </p>
      </div>

      <div className="rounded-md border p-4">
        <p className="text-sm font-medium">
          {t(`gateway.${settings.gatewayStatus}`)}
        </p>
        {settings.gatewayStatus === "unavailable" ? (
          <p className="mt-2 text-sm text-yellow-700">
            {t("gatewayUnavailableHint")}
          </p>
        ) : null}
      </div>

      <div>
        <label htmlFor="organization-default-model" className="mb-1 block text-sm font-medium">
          {t("defaultModel")}
        </label>
        <select
          id="organization-default-model"
          value={selected}
          onChange={(event) =>
            setDraft({ baseline: savedValue, value: event.target.value })
          }
          className="w-full rounded-md border px-3 py-2"
        >
          <option value="">{t("noDefault")}</option>
          {isStale ? (
            <option value={savedDefault ?? ""} disabled>
              {t("unavailable", { model: savedDefault ?? "" })}
            </option>
          ) : null}
          {settings.models.map((model) => (
            <option
              key={model.id}
              value={model.id}
              disabled={!model.organizationReadable}
            >
              {model.id}
            </option>
          ))}
        </select>
        <p className="mt-2 text-sm text-gray-600">
          {t("grantsHint")}
        </p>
        {savedDefault !== null && !settings.defaultAvailable ? (
          <p className="mt-2 text-sm text-yellow-700">
            {t("staleDefault")}
          </p>
        ) : null}
      </div>

      <div className="rounded-md border p-4">
        <label className="mb-1 flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={selectedTemperature !== null}
            onChange={(event) =>
              setTemperatureDraft({
                baseline: savedTemperature,
                value: event.target.checked
                  ? DEFAULT_TEMPERATURE_IF_ENABLED
                  : null,
              })
            }
          />
          {t("defaultTemperature")}
        </label>
        {selectedTemperature !== null ? (
          <div className="mt-2 flex max-w-xs items-center gap-3">
            <Slider
              value={selectedTemperature}
              min={0}
              max={2}
              step={0.1}
              onChange={(value) =>
                setTemperatureDraft({ baseline: savedTemperature, value })
              }
            />
            <span className="w-10 text-sm text-gray-600">
              {selectedTemperature.toFixed(1)}
            </span>
          </div>
        ) : (
          <p className="mt-2 text-sm text-gray-600">
            {t("temperatureUnset")}
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <button
          type="button"
          disabled={saveDisabled}
          onClick={() =>
            update.mutate(
              {
                defaultModel: selectedDefault,
                defaultTemperature: selectedTemperature,
              },
              {
                onSuccess: () => {
                  setDraft(null);
                  setTemperatureDraft(null);
                },
              }
            )
          }
          className="rounded-md bg-primary-600 px-4 py-2 text-white disabled:opacity-50"
        >
          {update.isPending ? t("saving") : t("save")}
        </button>
        <Link href="/admin/access" className="text-sm text-primary-700 underline">
          {t("configureAccess")}
        </Link>
      </div>

      {update.isError ? (
        <p className="text-sm text-red-600">{errorMessage(update.error)}</p>
      ) : null}
    </div>
  );
}
