"use client";

import { Badge } from "@/components/primitives/Badge";
import { Pill } from "@/components/primitives/Pill";
import { useAppShell } from "@/hooks/useAppShell";
import { useTranslations } from "next-intl";

export function ChatHeader() {
  const t = useTranslations("chat.header");
  const { agents, activeAgentId, setActiveConversationId } = useAppShell();
  const active = agents.find((a) => a.id === activeAgentId);

  return (
    <header className="flex items-center justify-between border-b border-aurum-border-soft bg-aurum-surface px-6 py-4">
      <div className="flex items-center gap-3">
        {active && <Badge color={active.badgeColor}>{active.badge}</Badge>}
        <div>
          <h1 className="text-base font-semibold text-aurum-text">
            {active?.name ?? t("fallbackTitle")}
          </h1>
          <p className="text-sm text-aurum-text-3">{active?.kindLabel}</p>
        </div>
      </div>
      <Pill onClick={() => setActiveConversationId(null)}>{t("newChat")}</Pill>
    </header>
  );
}
