import { Check, Loader2, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/primitives/Badge";
import type { ToolCall } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface ToolCallRowProps {
  tool: ToolCall;
}

const HERMES_PREFIX = "hermes.";

export function ToolCallRow({ tool }: ToolCallRowProps) {
  const t = useTranslations("chat.tool");
  const state = tool.state ?? "wait";
  // Hermes reports only which kind of tool ran, never its arguments or output.
  const hermesId = tool.toolId?.startsWith(HERMES_PREFIX)
    ? tool.toolId.slice(HERMES_PREFIX.length)
    : null;
  const hermesKey = `hermes.${hermesId}`;
  const name =
    hermesId === null ? tool.name : t.has(hermesKey) ? t(hermesKey) : t("hermes.other");
  const detail = hermesId === null ? tool.detail : "";

  return (
    <div
      className="flex items-center gap-3 rounded-lg border border-aurum-border-soft bg-aurum-surface px-3 py-2"
      aria-label={`${name}: ${t(state)}`}
    >
      <div
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
          state === "done"
            ? "bg-aurum-ok-bg text-aurum-ok-fg"
            : state === "run"
            ? "bg-aurum-accent-soft text-aurum-accent-ink"
            : "bg-aurum-surface-3 text-aurum-text-3"
        )}
      >
        {state === "done" ? (
          <Check className="h-4 w-4" />
        ) : state === "run" ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <Wrench className="h-4 w-4" />
        )}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium text-aurum-text">
            {name}
          </span>
          {tool.isAgent && tool.tag && tool.badgeColor && (
            <Badge color={tool.badgeColor} className="text-[10px]">
              {tool.tag}
            </Badge>
          )}
        </div>
        {detail ? <p className="truncate text-xs text-aurum-text-3">{detail}</p> : null}
      </div>

      <div className="shrink-0">
        {state === "done" ? (
          <Check className="h-4 w-4 text-aurum-ok-fg" />
        ) : state === "run" ? (
          <Loader2 className="h-4 w-4 animate-spin text-aurum-accent" />
        ) : (
          <span className="h-2 w-2 rounded-full bg-aurum-text-3/40" />
        )}
      </div>
    </div>
  );
}

