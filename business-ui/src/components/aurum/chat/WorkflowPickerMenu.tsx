"use client";

import { useMemo } from "react";
import { Bot, ChevronDown, Workflow } from "lucide-react";
import { Pill } from "@/components/primitives/Pill";
import { useChatAgents } from "@/hooks/useChatAgents";
import { useWorkflows } from "@/hooks/useWorkflows";
import type { ChatAgentId } from "@/lib/chat-api";
import { PickerMenu } from "./PickerMenu";
import { useTranslations } from "next-intl";

const ORDINARY_CHAT_ID = "__ordinary_chat__";
const HERMES_ID = "__hermes__";

/** What runs the next turn: the ordinary chat, a workflow, or an agent. */
export type ChatMode =
  | { kind: "chat" }
  | { kind: "workflow"; workflowId: string }
  | { kind: "agent"; agent: ChatAgentId };

export interface WorkflowPickerMenuProps {
  value: ChatMode;
  onChange: (mode: ChatMode) => void;
  disabled?: boolean;
}

export function WorkflowPickerMenu({
  value,
  onChange,
  disabled = false,
}: WorkflowPickerMenuProps) {
  const t = useTranslations("chat.pickers.workflow");
  const { data: workflows = [], isLoading } = useWorkflows({ enabled: !disabled });
  const { data: agents = [] } = useChatAgents({ enabled: !disabled });
  const hermes = agents.some((agent) => agent.id === "hermes");
  const activeWorkflows = useMemo(
    () => workflows.filter((workflow) => workflow.status === "active"),
    [workflows]
  );
  const selected =
    value.kind === "workflow"
      ? activeWorkflows.find((workflow) => workflow.id === value.workflowId)
      : undefined;
  const activeId =
    value.kind === "agent" ? HERMES_ID : (selected?.id ?? ORDINARY_CHAT_ID);
  const items = useMemo(
    () => [
      {
        id: ORDINARY_CHAT_ID,
        label: t("plainChat"),
        description: t("plainChatDescription"),
      },
      ...(hermes
        ? [{ id: HERMES_ID, label: t("hermes"), description: t("hermesDescription") }]
        : []),
      ...activeWorkflows.map((workflow) => ({
        id: workflow.id,
        label: workflow.name,
        description: workflow.description ?? undefined,
      })),
    ],
    [activeWorkflows, hermes, t]
  );

  return (
    <PickerMenu
      ariaLabel={t("choose")}
      activeId={activeId}
      onSelect={(id) =>
        onChange(
          id === ORDINARY_CHAT_ID
            ? { kind: "chat" }
            : id === HERMES_ID
              ? { kind: "agent", agent: "hermes" }
              : { kind: "workflow", workflowId: id }
        )
      }
      items={items}
      trigger={
        <Pill
          aria-label={t("choose")}
          disabled={disabled || isLoading}
          className="h-[34px] gap-[7px] rounded-input px-3 text-[13px] disabled:opacity-50"
        >
          {activeId === HERMES_ID ? (
            <Bot className="h-[14px] w-[14px]" />
          ) : (
            <Workflow className="h-[14px] w-[14px]" />
          )}
          {activeId === HERMES_ID ? t("hermes") : (selected?.name ?? t("plainChat"))}
          <ChevronDown className="h-[13px] w-[13px]" />
        </Pill>
      }
    />
  );
}
