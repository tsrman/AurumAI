"use client";

import { useMemo } from "react";
import { ChevronDown, Workflow } from "lucide-react";
import { Pill } from "@/components/primitives/Pill";
import { useWorkflows } from "@/hooks/useWorkflows";
import { PickerMenu } from "./PickerMenu";
import { useTranslations } from "next-intl";

const ORDINARY_CHAT_ID = "__ordinary_chat__";

export interface WorkflowPickerMenuProps {
  value: string | null;
  onChange: (workflowId: string | null) => void;
  disabled?: boolean;
}

export function WorkflowPickerMenu({
  value,
  onChange,
  disabled = false,
}: WorkflowPickerMenuProps) {
  const t = useTranslations("chat.pickers.workflow");
  const { data: workflows = [], isLoading } = useWorkflows({ enabled: !disabled });
  const activeWorkflows = useMemo(
    () => workflows.filter((workflow) => workflow.status === "active"),
    [workflows]
  );
  const selected = activeWorkflows.find((workflow) => workflow.id === value);
  const items = useMemo(
    () => [
      {
        id: ORDINARY_CHAT_ID,
        label: t("plainChat"),
        description: t("plainChatDescription"),
      },
      ...activeWorkflows.map((workflow) => ({
        id: workflow.id,
        label: workflow.name,
        description: workflow.description ?? undefined,
      })),
    ],
    [activeWorkflows, t]
  );

  return (
    <PickerMenu
      ariaLabel={t("choose")}
      activeId={value ?? ORDINARY_CHAT_ID}
      onSelect={(id) => onChange(id === ORDINARY_CHAT_ID ? null : id)}
      items={items}
      trigger={
        <Pill
          aria-label={t("choose")}
          disabled={disabled || isLoading}
          className="h-[34px] gap-[7px] rounded-input px-3 text-[13px] disabled:opacity-50"
        >
          <Workflow className="h-[14px] w-[14px]" />
          {selected?.name ?? t("plainChat")}
          <ChevronDown className="h-[13px] w-[13px]" />
        </Pill>
      }
    />
  );
}
