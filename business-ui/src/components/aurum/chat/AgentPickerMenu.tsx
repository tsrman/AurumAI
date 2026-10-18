"use client";

import { useMemo } from "react";
import { ChevronDown } from "lucide-react";
import { Badge } from "@/components/primitives/Badge";
import { Pill } from "@/components/primitives/Pill";
import { useAppShell } from "@/hooks/useAppShell";
import { PickerMenu } from "./PickerMenu";
import { useTranslations } from "next-intl";

export function AgentPickerMenu() {
  const t = useTranslations("chat.pickers.agent");
  const { agents, myAgents, activeAgentId, setActiveAgentId } = useAppShell();
  const allAgents = useMemo(() => [...agents, ...myAgents], [agents, myAgents]);
  const activeAgent = allAgents.find((a) => a.id === activeAgentId);

  const items = useMemo(
    () =>
      allAgents.map((agent) => ({
        id: agent.id,
        label: agent.name,
        description: agent.description,
        badge: <Badge color={agent.badgeColor}>{agent.badge}</Badge>,
      })),
    [allAgents]
  );

  return (
    <PickerMenu
      ariaLabel={t("choose")}
      activeId={activeAgentId}
      onSelect={setActiveAgentId}
      items={items}
      trigger={
        <Pill className="h-[34px] gap-[7px] rounded-input px-3 text-[13px]">
          {activeAgent && (
            <Badge
              color={activeAgent.badgeColor}
              className="h-[18px] w-[18px] rounded-[5px] px-0 py-0 text-[8px]"
            >
              {activeAgent.badge}
            </Badge>
          )}
          {activeAgent?.name ?? t("fallback")}
          <ChevronDown className="h-[13px] w-[13px]" />
        </Pill>
      }
    />
  );
}
