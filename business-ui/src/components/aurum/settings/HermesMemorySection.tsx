"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/primitives/Button";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { SettingsCard } from "./SettingsCard";
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
    <SettingsCard id="hermes-memory-title" title={t("title")} description={t("description")}>
      {memory.isError ? <p className="text-sm text-aurum-err-fg">{t("loadFailed")}</p> : null}
      {data && !empty ? (
        <>
          {(["memory", "user"] as const).map((kind) =>
            data[kind].trim() ? (
              <div key={kind}>
                <h3 className="mb-1 text-sm font-medium text-aurum-text">{t(kind)}</h3>
                <pre className="whitespace-pre-wrap rounded-input border border-aurum-border-soft bg-aurum-surface-2 p-3 text-xs text-aurum-text-2">
                  {data[kind]}
                </pre>
              </div>
            ) : null
          )}
          <Button variant="danger" onClick={() => setConfirming(true)} disabled={clear.isPending}>
            {t("clear")}
          </Button>
        </>
      ) : data ? (
        <p className="text-sm text-aurum-text-3">{t("empty")}</p>
      ) : null}
      {clear.isError ? <p className="text-sm text-aurum-err-fg">{t("clearFailed")}</p> : null}
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
    </SettingsCard>
  );
}
