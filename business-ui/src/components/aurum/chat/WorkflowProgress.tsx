"use client";

import { useState } from "react";
import { Check, Circle, LoaderCircle, MinusCircle, UserCheck, X } from "lucide-react";
import type {
  WorkflowNodeProgress,
  WorkflowNodeStatus,
  WorkflowProgressState,
} from "@/types/aurum";
import { decideWorkflowNode } from "@/lib/workflows-api";
import { useTranslations } from "next-intl";

// Workflow error codes with a dedicated message in `workflow.errors`.
const ERROR_CODES = new Set([
  "access_revoked",
  "quota_exhausted",
  "execution_failed",
  "output_limit_exceeded",
  "invalid_supervisor_output",
  "invalid_workflow_snapshot",
  "workflow_unavailable",
  "runner_unavailable",
  "human_rejected",
  "confirmation_timeout",
]);

function StatusIcon({ status }: { status: WorkflowNodeStatus }) {
  if (status === "running") {
    return <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />;
  }
  if (status === "waiting_human") {
    return <UserCheck className="h-3.5 w-3.5" aria-hidden="true" />;
  }
  if (status === "completed") {
    return <Check className="h-3.5 w-3.5" aria-hidden="true" />;
  }
  if (status === "failed" || status === "cancelled") {
    return <X className="h-3.5 w-3.5" aria-hidden="true" />;
  }
  if (status === "skipped") {
    return <MinusCircle className="h-3.5 w-3.5" aria-hidden="true" />;
  }
  return <Circle className="h-3.5 w-3.5" aria-hidden="true" />;
}

function DecisionButtons({ runId, nodeKey }: { runId: string; nodeKey: string }) {
  const t = useTranslations("workflow");
  const [state, setState] = useState<"idle" | "pending" | "sent" | "error">("idle");

  const decide = async (decision: "approve" | "reject") => {
    setState("pending");
    try {
      await decideWorkflowNode(runId, nodeKey, decision);
      setState("sent");
    } catch {
      setState("error");
    }
  };

  if (state === "sent") {
    return <span className="text-aurum-text-3">{t("decisionSent")}</span>;
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      {state === "error" && (
        <span className="text-aurum-err-fg">{t("decisionFailed")}</span>
      )}
      <button
        type="button"
        aria-label={t("approveAria", { node: nodeKey })}
        disabled={state === "pending"}
        onClick={() => void decide("approve")}
        className="rounded px-2 py-0.5 text-aurum-text-2 hover:bg-aurum-surface-3 disabled:opacity-50"
      >
        {t("approve")}
      </button>
      <button
        type="button"
        aria-label={t("rejectAria", { node: nodeKey })}
        disabled={state === "pending"}
        onClick={() => void decide("reject")}
        className="rounded px-2 py-0.5 text-aurum-err-fg hover:bg-aurum-surface-3 disabled:opacity-50"
      >
        {t("reject")}
      </button>
    </span>
  );
}

function NodeRow({ node, runId }: { node: WorkflowNodeProgress; runId: string }) {
  const t = useTranslations("workflow");
  return (
    <li className="flex items-center justify-between gap-4 text-xs">
      <span className="min-w-0 truncate text-aurum-text-2">{node.nodeName}</span>
      {node.status === "waiting_human" ? (
        <DecisionButtons runId={runId} nodeKey={node.nodeKey} />
      ) : (
        <span className="inline-flex shrink-0 items-center gap-1 text-aurum-text-3">
          <StatusIcon status={node.status} />
          {t(`node.${node.status}`)}
        </span>
      )}
    </li>
  );
}

export function WorkflowProgress({ progress }: { progress: WorkflowProgressState }) {
  const t = useTranslations("workflow");
  const nodes = Object.values(progress.nodes).sort((left, right) =>
    left.nodeKey.localeCompare(right.nodeKey)
  );
  const errorMessage = progress.errorCode
    ? t(`errors.${ERROR_CODES.has(progress.errorCode) ? progress.errorCode : "execution_failed"}`)
    : null;

  return (
    <section
      aria-label={t("progress")}
      className="rounded-badge border border-aurum-border bg-aurum-surface-2 px-3 py-2.5"
    >
      <div className="text-sm font-semibold text-aurum-text">
        {t(`run.${progress.status}`)}
      </div>
      {nodes.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {nodes.map((node) => (
            <NodeRow key={node.nodeKey} node={node} runId={progress.workflowRunId} />
          ))}
        </ul>
      )}
      {errorMessage && (
        <p className="mt-2 text-xs text-aurum-err-fg">{errorMessage}</p>
      )}
    </section>
  );
}
