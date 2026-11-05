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
import { Button } from "@/components/primitives/Button";
import { Toggle } from "@/components/primitives/Toggle";
import { SettingsCard } from "@/components/aurum/settings/SettingsCard";
import { HermesMemorySection } from "@/components/aurum/settings/HermesMemorySection";
import { HermesSkillDraftsSection } from "@/components/aurum/settings/HermesSkillDraftsSection";

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
    return <div className="p-8 text-sm text-aurum-text-3">{t("loading")}</div>;
  }

  const toggles = [
    { key: "enter_to_send", label: t("sendOnEnter") },
    { key: "show_timestamps", label: t("showTimestamps") },
    { key: "sidebar_collapsed", label: t("collapseSidebar") },
  ] as const;

  return (
    <div className="flex h-full flex-1 flex-col">
      <header className="border-b border-aurum-border-soft bg-aurum-surface px-6 py-4">
        <h1 className="text-base font-semibold text-aurum-text">{t("title")}</h1>
      </header>
      <main className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto w-full max-w-2xl space-y-6">
          <form onSubmit={handleSubmit}>
            <SettingsCard>
              <div>
                <label
                  htmlFor="default-model"
                  className="mb-1.5 block text-sm font-medium text-aurum-text-2"
                >
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
                  className="w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text outline-none transition focus:border-aurum-accent focus:ring-2 focus:ring-aurum-accent/20"
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

              <ul className="divide-y divide-aurum-border-soft">
                {toggles.map(({ key, label }) => (
                  <li key={key} className="flex items-center justify-between gap-4 py-3">
                    <span id={`setting-${key}`} className="text-sm text-aurum-text">
                      {label}
                    </span>
                    <Toggle
                      aria-labelledby={`setting-${key}`}
                      checked={!!form[key]}
                      onChange={(value) => setEdits({ ...edits, [key]: value })}
                    />
                  </li>
                ))}
              </ul>

              <div className="flex items-center gap-4">
                <Button
                  type="submit"
                  disabled={update.isPending || Object.keys(edits).length === 0}
                >
                  {update.isPending ? t("saving") : t("save")}
                </Button>
                {update.isSuccess && (
                  <p role="status" className="text-sm text-aurum-ok-fg">{t("saved")}</p>
                )}
                {update.isError && (
                  <p role="alert" className="text-sm text-aurum-err-fg">{t("saveFailed")}</p>
                )}
              </div>
            </SettingsCard>
          </form>
          <HermesMemorySection />
          <HermesSkillDraftsSection />
        </div>
      </main>
    </div>
  );
}
