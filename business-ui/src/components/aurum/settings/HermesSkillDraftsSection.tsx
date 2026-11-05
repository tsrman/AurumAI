"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/primitives/Button";
import { SettingsCard } from "./SettingsCard";
import { useChatAgents } from "@/hooks/useChatAgents";
import { useHermesSkillDrafts, useWithdrawHermesSkillDraft } from "@/hooks/useHermesSkillDrafts";

/** Skills Hermes Agent wrote for itself; an administrator decides whether
 * they become platform skills. Only shown where Hermes can be used. */
export function HermesSkillDraftsSection() {
  const t = useTranslations("settings.hermesSkills");
  const { data: agents = [] } = useChatAgents();
  const available = agents.some((agent) => agent.id === "hermes");
  const drafts = useHermesSkillDrafts({ enabled: available });
  const withdraw = useWithdrawHermesSkillDraft();

  if (!available) return null;

  return (
    <SettingsCard id="hermes-skills-title" title={t("title")} description={t("description")}>
      {drafts.isError ? <p className="text-sm text-aurum-err-fg">{t("loadFailed")}</p> : null}
      {drafts.data && drafts.data.length === 0 ? (
        <p className="text-sm text-aurum-text-3">{t("empty")}</p>
      ) : null}
      {drafts.data && drafts.data.length > 0 ? (
        <ul className="divide-y divide-aurum-border-soft rounded-input border border-aurum-border-soft">
          {drafts.data.map((draft) => (
            <li key={draft.id} className="flex items-start justify-between gap-4 p-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-aurum-text">{draft.name}</p>
                <p className="text-xs text-aurum-text-3">{draft.description}</p>
                <p className="mt-1 text-xs text-aurum-text-3">{t(`status.${draft.status}`)}</p>
              </div>
              {draft.status === "pending" ? (
                <Button
                  variant="secondary"
                  onClick={() => withdraw.mutate(draft.id)}
                  disabled={withdraw.isPending}
                  aria-label={t("withdrawAria", { name: draft.name })}
                  className="shrink-0 px-3 py-1 text-xs"
                >
                  {t("withdraw")}
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {withdraw.isError ? <p className="text-sm text-aurum-err-fg">{t("withdrawFailed")}</p> : null}
    </SettingsCard>
  );
}
