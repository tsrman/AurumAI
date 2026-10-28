"use client";

import { useState } from "react";
import {
  useSettings,
  useUpdateSettings,
  UserSettingsUpdate,
} from "@/hooks/useSettings";
import { useModels } from "@/hooks/useModels";
import { useAuth } from "@/providers/AuthProvider";
import { useTranslations } from "next-intl";
import { HermesMemorySection } from "@/components/aurum/settings/HermesMemorySection";

export default function SettingsPage() {
  const t = useTranslations("settings");
  const { isAuthenticated } = useAuth();
  const { data, isLoading } = useSettings({ enabled: isAuthenticated });
  const { data: modelsData, isLoading: modelsLoading } = useModels({ enabled: isAuthenticated });
  const update = useUpdateSettings();
  // Local edits layered over whatever the server last returned, instead of
  // copying the fetched settings into state from an effect: the effect
  // re-ran on every refetch and could overwrite a field the user had just
  // changed, and React 19's react-hooks/set-state-in-effect flags exactly
  // this shape.
  const [edits, setEdits] = useState<UserSettingsUpdate>({});
  const form = { ...(data?.data ?? {}), ...edits };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (Object.keys(edits).length === 0) return;
    await update.mutateAsync(edits);
    setEdits({});
  };

  const models = modelsData?.data || [];
  // A model can be revoked, or leave the gateway, after it was saved as the
  // personal default. Without an option of its own the browser shows the
  // first entry (the organization default), while the stored value is
  // still that model — and an untouched form keeps Save disabled, so there
  // was no way to clear it from here.
  const storedModel = form.default_model ?? "";
  const storedModelIsMissing =
    storedModel !== "" && !models.some((m) => m.id === storedModel);

  // before auth resolves the queries above are
  // disabled (never fetch, so react-query's own isLoading reads false) —
  // without this, a cold-loaded /settings briefly rendered the form with
  // no data instead of a loading state.
  if (!isAuthenticated || isLoading || modelsLoading) {
    return <div className="p-8">{t("loading")}</div>;
  }

  return (
    <div className="mx-auto max-w-xl p-8">
      <h1 className="mb-6 text-2xl font-semibold">{t("title")}</h1>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="default-model" className="mb-1 block text-sm font-medium">
            {t("defaultModel")}
          </label>
          <select
            id="default-model"
            value={storedModel}
            onChange={(e) =>
              setEdits({
                ...edits,
                default_model: e.target.value || null,
              })
            }
            className="w-full rounded-md border px-3 py-2"
          >
            <option value="">
              {t("organizationDefault")}
              {form.organization_default_model
                ? ` (${form.organization_default_model})`
                : ""}
            </option>
            {storedModelIsMissing ? (
              <option value={storedModel}>{t("unavailableModel", { model: storedModel })}</option>
            ) : null}
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}
              </option>
            ))}
          </select>
        </div>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={!!form.enter_to_send}
            onChange={(e) => setEdits({ ...edits, enter_to_send: e.target.checked })}
          />
          <span className="text-sm">{t("sendOnEnter")}</span>
        </label>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={!!form.show_timestamps}
            onChange={(e) => setEdits({ ...edits, show_timestamps: e.target.checked })}
          />
          <span className="text-sm">{t("showTimestamps")}</span>
        </label>

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={!!form.sidebar_collapsed}
            onChange={(e) => setEdits({ ...edits, sidebar_collapsed: e.target.checked })}
          />
          <span className="text-sm">{t("collapseSidebar")}</span>
        </label>

        <button
          type="submit"
          disabled={update.isPending || Object.keys(edits).length === 0}
          className="rounded-md bg-primary-600 px-4 py-2 text-white disabled:opacity-50"
        >
          {update.isPending ? t("saving") : t("save")}
        </button>

        {update.isSuccess && (
          <p className="text-sm text-green-600">{t("saved")}</p>
        )}
        {update.isError && (
          <p className="text-sm text-red-600">{t("saveFailed")}</p>
        )}
      </form>
      <HermesMemorySection />
    </div>
  );
}
