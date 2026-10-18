"use client";

import { useMemo } from "react";
import { ChevronDown } from "lucide-react";
import { Pill } from "@/components/primitives/Pill";
import { useAppShell } from "@/hooks/useAppShell";
import { PickerMenu } from "./PickerMenu";
import { useTranslations } from "next-intl";

export function ModelPickerMenu() {
  const t = useTranslations("chat.pickers.model");
  const { models, activeModelId, setActiveModelId } = useAppShell();
  const activeModel = models.find((m) => m.id === activeModelId);

  const items = useMemo(
    () =>
      models.map((model) => ({
        id: model.id,
        label: model.name,
        description: model.description,
      })),
    [models]
  );

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
