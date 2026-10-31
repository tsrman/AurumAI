"use client";

import { useCallback, useMemo } from "react";
import { AlertTriangle, Copy, Check, RotateCcw, ShieldCheck, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/primitives/Badge";
import { IconButton } from "@/components/primitives/IconButton";
import { useAppShell } from "@/hooks/useAppShell";
import { ThinkingBox } from "./ThinkingBox";
import { PendingPulse } from "./PendingPulse";
import { Markdown } from "./Markdown";
import { ToolCallRow } from "./ToolCallRow";
import { SourceCitations } from "./SourceCitations";
import { ConversationImages } from "./ConversationImages";
import { ConversationFiles } from "./ConversationFiles";
import { ConversationPreviews } from "./ConversationPreviews";
import { WorkflowProgress } from "./WorkflowProgress";
import { MetricsWidget } from "./widgets/MetricsWidget";
import { BarsWidget } from "./widgets/BarsWidget";
import { TableWidget } from "./widgets/TableWidget";
import { SqlBlock } from "./widgets/SqlBlock";
import type { AssistantMessageView, Widget } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface AssistantBlockProps {
  message: AssistantMessageView;
  onRegenerate?: (messageId: string) => void;
}

function FaithfulnessBadge({
  score,
  reason,
}: {
  score: number;
  reason?: string | null;
}) {
  const t = useTranslations("chat.assistant");
  const verified = score >= 0.5;
  return (
    <span
      className={
        verified
          ? "inline-flex items-center gap-1 rounded-badge bg-aurum-ok-bg px-2 py-0.5 text-xs font-medium text-aurum-ok-fg"
          : "inline-flex items-center gap-1 rounded-badge bg-aurum-err-bg px-2 py-0.5 text-xs font-medium text-aurum-err-fg"
      }
      title={reason || undefined}
    >
      {verified ? (
        <ShieldCheck className="h-3.5 w-3.5" />
      ) : (
        <ShieldAlert className="h-3.5 w-3.5" />
      )}
      {verified ? t("verified") : t("mayBeInaccurate")}
    </span>
  );
}

function WidgetRenderer({ widget }: { widget: Widget }) {
  switch (widget.type) {
    case "metrics":
      return <MetricsWidget widget={widget} />;
    case "bars":
      return <BarsWidget widget={widget} />;
    case "table":
      return <TableWidget widget={widget} />;
    default:
      return null;
  }
}

export function AssistantBlock({ message, onRegenerate }: AssistantBlockProps) {
  const t = useTranslations("chat.assistant");
  const { agents, myAgents } = useAppShell();
  const [copied, setCopied] = useState(false);

  // Gap between placing the placeholder message and the first SSE event
  // (message-start/thinking-delta/tool-calls/content-delta) — nothing else
  // in this component renders yet, so without this the user has no
  // confirmation their message actually reached the platform/model.
  const isPending =
    message.phase === "tools" &&
    message.tools.length === 0 &&
    !message.thinking &&
    !message.contentShown &&
    !message.workflow;

  const agent = useMemo(
    () =>
      [...agents, ...myAgents].find((a) => a.id === message.agentId) ??
      agents[0],
    [agents, myAgents, message.agentId]
  );

  const handleCopy = useCallback(() => {
    if (!message.contentShown) return;
    void navigator.clipboard.writeText(message.contentShown).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, [message.contentShown]);

  const titleId = `assistant-title-${message.id}`;

  return (
    <div className="flex justify-start">
      <article
        className="max-w-3xl space-y-3"
        aria-labelledby={titleId}
        data-testid="assistant-message"
      >
        <div id={titleId} className="flex items-center gap-2">
          {agent && <Badge color={agent.badgeColor}>{agent.badge}</Badge>}
          <span className="text-sm font-semibold text-aurum-text">
            {agent?.name ?? t("fallbackName")}
          </span>
          <span className="text-xs text-aurum-text-3">{agent?.kindLabel}</span>
        </div>

        {message.workflow && <WorkflowProgress progress={message.workflow} />}

        {isPending && <PendingPulse />}

        {message.thinking && <ThinkingBox message={message} />}

        {message.tools.length > 0 && (
          <div className="flex flex-col gap-2">
            {message.tools.map((tool, index) => (
              <ToolCallRow key={`${tool.name}-${index}`} tool={tool} />
            ))}
          </div>
        )}

        {message.contentShown && message.failed ? (
          // The reason a turn failed, not an answer: plain text, set apart.
          <div className="flex gap-2 rounded-card border border-aurum-err-fg/30 bg-aurum-err-bg px-3 py-2 text-sm text-aurum-err-fg">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <div>
              <p className="font-semibold">{t("failed")}</p>
              <p data-testid="assistant-content" className="whitespace-pre-wrap">
                {message.contentShown}
              </p>
            </div>
          </div>
        ) : message.contentShown ? (
          <div data-testid="assistant-content">
            <Markdown
              content={message.contentShown}
              streaming={message.phase === "answer"}
            />
          </div>
        ) : null}

        {message.images && message.images.length > 0 && (
          <ConversationImages imageIds={message.images} />
        )}

        {message.files && message.files.length > 0 && <ConversationFiles files={message.files} />}

        {message.previews && message.previews.length > 0 && <ConversationPreviews previews={message.previews} />}

        {message.widgets.length > 0 && (
          <div className="flex flex-col gap-3">
            {message.widgets.map((widget, index) => (
              <WidgetRenderer key={index} widget={widget} />
            ))}
          </div>
        )}

        {message.sql && <SqlBlock sql={message.sql} />}

        {message.sources.length > 0 && <SourceCitations sources={message.sources} />}

        <div className="flex items-center gap-2">
          {message.failed ? null : (
            <IconButton
              bare
              aria-label={t("copy")}
              onClick={handleCopy}
            >
              {copied ? (
                <Check className="h-4 w-4" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </IconButton>
          )}
          {message.failed ? (
            <button
              type="button"
              onClick={() => onRegenerate?.(message.id)}
              className="inline-flex items-center gap-1.5 rounded-btn px-2 py-1 text-xs font-medium text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              {t("tryAgain")}
            </button>
          ) : (
            <IconButton
              bare
              aria-label={t("regenerate")}
              onClick={() => onRegenerate?.(message.id)}
            >
              <RotateCcw className="h-4 w-4" />
            </IconButton>
          )}
          {message.phase === "done" &&
            typeof message.faithfulnessScore === "number" && (
              <FaithfulnessBadge
                score={message.faithfulnessScore}
                reason={message.faithfulnessReason}
              />
            )}
        </div>
      </article>
    </div>
  );
}
