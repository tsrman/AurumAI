"use client";

import { ChevronDown } from "lucide-react";
import { PickerMenu } from "@/components/aurum/chat/PickerMenu";
import type { Model } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface ModelPickerFieldProps {
  value: string;
  options: Model[];
  onChange: (modelId: string) => void;
}

export function ModelPickerField({ value, options, onChange }: ModelPickerFieldProps) {
  const t = useTranslations("constructor");
  const active = options.find((model) => model.id === value);

  const items = options.map((model) => ({
    id: model.id,
    label: model.name,
    description: model.description || undefined,
  }));

  return (
    <PickerMenu
      ariaLabel={t("chooseModel")}
      activeId={value}
      onSelect={onChange}
      items={items}
      trigger={
        <button
          type="button"
          className="flex w-full items-center justify-between gap-2 rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-left text-sm text-aurum-text transition hover:bg-aurum-surface-3 focus:outline-none focus:ring-2 focus:ring-aurum-accent/50"
        >
          <span className={active ? "truncate" : "truncate text-aurum-text-3"}>
            {active?.shortName ?? active?.name ?? (value || t("chooseModel"))}
          </span>
          <ChevronDown className="h-4 w-4 shrink-0 text-aurum-text-3" />
        </button>
      }
    />
  );
}
