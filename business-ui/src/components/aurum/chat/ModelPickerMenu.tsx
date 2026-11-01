"use client";

import { useMemo } from "react";
import { ChevronDown } from "lucide-react";
import { Pill } from "@/components/primitives/Pill";
import { useAppShell } from "@/hooks/useAppShell";
import { PickerMenu } from "./PickerMenu";
import { useTranslations } from "next-intl";

export function ModelPickerMenu() {
  const t = useTranslations("chat.pickers.model");
  const { models, activeModelId, setActiveModelId, agents, myAgents, activeAgentId } = useAppShell();
  const activeModel = models.find((m) => m.id === activeModelId);
  // An administrator's agent fixes the model: shown, not chosen.
  const agentModel = [...agents, ...myAgents].find(
    (agent) => agent.id === activeAgentId && agent.kind === "definition"
  )?.model;

  const items = useMemo(
    () =>
      models.map((model) => ({
        id: model.id,
        label: model.name,
        description: model.description,
      })),
    [models]
  );

  if (agentModel) {
    return (
      <Pill
        aria-label={t("fixedByAgent", { model: agentModel })}
        title={t("fixedByAgent", { model: agentModel })}
        className="h-[34px] gap-[7px] rounded-input px-3 text-[13px] opacity-80"
      >
        {agentModel}
      </Pill>
    );
  }

  return (
    <PickerMenu
      ariaLabel={t("choose")}
      activeId={activeModelId}
      onSelect={setActiveModelId}
      items={items}
      trigger={
        <Pill
          aria-label={t("choose")}
          className="h-[34px] gap-[7px] rounded-input px-3 text-[13px]"
        >
          {activeModel?.shortName ?? activeModel?.name ?? t("fallback")}
          <ChevronDown className="h-[13px] w-[13px]" />
        </Pill>
      }
    />
  );
}
