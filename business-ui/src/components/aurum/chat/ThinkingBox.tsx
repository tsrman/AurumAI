"use client";

import { useState } from "react";
import { ChevronDown, Loader2, Sparkles } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AssistantMessageView } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface ThinkingBoxProps {
  message: AssistantMessageView;
}

export function ThinkingBox({ message }: ThinkingBoxProps) {
  const t = useTranslations("chat.thinking");
  const [open, setOpen] = useState(message.thinkingOpen);

  const isThinking = message.phase === "think";
  const isDone = message.phase === "done";
  const panelId = `thinking-panel-${message.id}`;

  return (
    <div className="rounded-card border border-aurum-border bg-aurum-surface-2">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-controls={panelId}
        className="flex w-full items-center justify-between px-4 py-2.5 text-left"
      >
        <div className="flex items-center gap-2">
          {isDone ? (
            <Sparkles className="h-4 w-4 text-aurum-accent" />
          ) : (
            <Loader2
              className={cn(
                "h-4 w-4 text-aurum-accent",
                isThinking && "animate-spin"
              )}
            />
          )}
          <span className="text-sm font-medium text-aurum-text-2">
            {t("title")}
          </span>
          {isDone && message.thinkingSeconds > 0 && (
            <span className="text-xs text-aurum-text-3">
              {t("seconds", { seconds: message.thinkingSeconds })}
            </span>
          )}
        </div>
        <ChevronDown
          className={cn(
            "h-4 w-4 text-aurum-text-3 transition-transform",
            open && "rotate-180"
          )}
        />
      </button>
      {open && (
        <div id={panelId} className="border-t border-aurum-border px-4 py-3">
          <p className="whitespace-pre-wrap text-sm text-aurum-text-2">
            {message.thinkingShown || (
              <span className="inline-flex items-center gap-2">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                {t("inProgress")}
              </span>
            )}
          </p>
        </div>
      )}
    </div>
  );
}
