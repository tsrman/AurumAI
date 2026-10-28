"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { useChatAgents } from "@/hooks/useChatAgents";
import { useClearHermesMemory, useHermesMemory } from "@/hooks/useHermesMemory";

/** What Hermes Agent remembers about the user across conversations; only
 * shown where Hermes can be used. */
export function HermesMemorySection() {
  const t = useTranslations("settings.hermesMemory");
  const { data: agents = [] } = useChatAgents();
  const available = agents.some((agent) => agent.id === "hermes");
  const memory = useHermesMemory({ enabled: available });
  const clear = useClearHermesMemory();
  const [confirming, setConfirming] = useState(false);

  if (!available) return null;
  const data = memory.data;
  const empty = !data || (!data.memory.trim() && !data.user.trim());

  return (
    <section aria-labelledby="hermes-memory-title" className="mt-10 space-y-3">
      <h2 id="hermes-memory-title" className="text-lg font-semibold">
        {t("title")}
      </h2>
      <p className="text-sm text-aurum-text-3">{t("description")}</p>
      {memory.isError ? <p className="text-sm text-red-600">{t("loadFailed")}</p> : null}
      {data && !empty ? (
        <>
          {(["memory", "user"] as const).map((kind) =>
            data[kind].trim() ? (
              <div key={kind}>
                <h3 className="mb-1 text-sm font-medium">{t(kind)}</h3>
                <pre className="whitespace-pre-wrap rounded-md border border-aurum-border-soft bg-aurum-surface p-3 text-xs">
                  {data[kind]}
                </pre>
              </div>
            ) : null
          )}
          <button
            type="button"
            onClick={() => setConfirming(true)}
            disabled={clear.isPending}
            className="rounded-md border border-red-600 px-4 py-2 text-sm text-red-600 disabled:opacity-50"
          >
            {t("clear")}
          </button>
        </>
      ) : data ? (
        <p className="text-sm text-aurum-text-3">{t("empty")}</p>
      ) : null}
      {clear.isError ? <p className="text-sm text-red-600">{t("clearFailed")}</p> : null}
      <ConfirmDialog
        open={confirming}
        title={t("confirmTitle")}
        description={t("confirmDescription")}
        confirmLabel={t("clear")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          clear.mutate();
        }}
      />
    </section>
  );
}
