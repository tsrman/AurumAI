"use client";

import { useState } from "react";

import {
  useArchiveWorkflowTemplate,
  useCaptureWorkflowTemplate,
  useInstantiateWorkflowTemplate,
  useReplaceWorkflowTemplate,
  useWorkflowTemplate,
  useWorkflowTemplates,
} from "@/hooks/useWorkflowTemplates";
import { useWorkflow } from "@/hooks/useWorkflows";
import type { WorkflowListItem } from "@/lib/api";
import { useTranslations } from "next-intl";

type Props = {
  workflows: WorkflowListItem[];
  sourceWorkflowId: string | null;
  onWorkflowCreated: (id: string) => void;
};

async function errorCode(cause: unknown): Promise<{
  code: string | null;
  status: number | null;
}> {
  if (typeof cause !== "object" || cause === null || !("response" in cause)) {
    return { code: null, status: null };
  }
  const response = (cause as { response?: unknown }).response;
  if (!(response instanceof Response)) return { code: null, status: null };
  const body = (await response.clone().json().catch(() => null)) as
    | { detail?: string | { code?: string }; error?: { code?: string } }
    | null;
  return {
    code:
      (typeof body?.detail === "object" ? body.detail.code : body?.detail) ??
      body?.error?.code ??
      null,
    status: response.status,
  };
}

type TemplateErrorKey =
  | "fallback"
  | "conflict"
  | "network"
  | "session"
  | "template_not_found"
  | "source_not_found"
  | "dependency_unavailable"
  | "catalog_unavailable"
  | "snapshot_invalid";

const KNOWN_ERROR_CODES = new Set<TemplateErrorKey>([
  "template_not_found",
  "source_not_found",
  "dependency_unavailable",
  "catalog_unavailable",
  "snapshot_invalid",
]);

async function safeError(cause: unknown): Promise<{
  messageKey: TemplateErrorKey;
  conflict: boolean;
}> {
  const { code, status } = await errorCode(cause);
  if (status === 409) return { messageKey: "conflict", conflict: true };
  if (status === null) return { messageKey: "network", conflict: false };
  if (status === 401) return { messageKey: "session", conflict: false };
  const known = code !== null && KNOWN_ERROR_CODES.has(code as TemplateErrorKey);
  return { messageKey: known ? (code as TemplateErrorKey) : "fallback", conflict: false };
}

function isFailedRefetch(result: unknown) {
  return (
    typeof result === "object" &&
    result !== null &&
    "isError" in result &&
    result.isError === true
  );
}

export function WorkflowTemplatesPanel({
  workflows,
  sourceWorkflowId,
  onWorkflowCreated,
}: Props) {
  const t = useTranslations("workflows.templates");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [templateName, setTemplateName] = useState("");
  const [copyName, setCopyName] = useState("");
  const [localPending, setLocalPending] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copyNotice, setCopyNotice] = useState(false);
  const [archivedIds, setArchivedIds] = useState<Set<string>>(() => new Set());

  const templates = useWorkflowTemplates(includeArchived);
  const effectiveSelectedId = selectedId ?? templates.data?.data[0]?.id ?? null;
  const listSelection = templates.data?.data.find(
    (item) => item.id === effectiveSelectedId
  );
  const detail = useWorkflowTemplate(
    effectiveSelectedId,
    listSelection?.version
  );
  const source = useWorkflow(sourceWorkflowId);
  // A save invalidates the source query; its cached version is stale until the
  // refetch lands, and writing with it would be rejected as a version conflict.
  const sourceReady = Boolean(source.data?.data) && !source.isFetching;
  const capture = useCaptureWorkflowTemplate();
  const replace = useReplaceWorkflowTemplate();
  const instantiate = useInstantiateWorkflowTemplate();
  const archive = useArchiveWorkflowTemplate();
  const selected = detail.data?.data;
  const busy =
    localPending ||
    capture.isPending ||
    replace.isPending ||
    instantiate.isPending ||
    archive.isPending;
  const selectedArchived =
    selected?.status === "archived" ||
    listSelection?.status === "archived" ||
    (effectiveSelectedId !== null && archivedIds.has(effectiveSelectedId));
  const selectedWriteDisabled =
    busy || conflict || !selected || selectedArchived;

  const run = async (operation: () => Promise<void>) => {
    if (busy || conflict) return;
    setLocalPending(true);
    setError(null);
    setCopyNotice(false);
    try {
      await operation();
    } catch (cause) {
      const safe = await safeError(cause);
      setError(t(`errors.${safe.messageKey}`));
      setConflict(safe.conflict);
    } finally {
      setLocalPending(false);
    }
  };

  const refreshAfterConflict = async () => {
    setLocalPending(true);
    setError(null);
    try {
      const refreshes: Promise<unknown>[] = [templates.refetch()];
      if (effectiveSelectedId !== null) refreshes.push(detail.refetch());
      if (sourceWorkflowId !== null) refreshes.push(source.refetch());
      const results = await Promise.all(refreshes);
      if (results.some(isFailedRefetch)) {
        setError(t("refreshFailed"));
        return;
      }
      setConflict(false);
    } catch {
      setError(t("refreshFailed"));
    } finally {
      setLocalPending(false);
    }
  };

  return (
    <section className="space-y-4 rounded-card border border-aurum-border bg-aurum-surface p-5">
      <div>
        <h2 className="text-lg font-semibold">{t("title")}</h2>
        <p className="text-sm text-aurum-text-2">
          {t("subtitle")}
        </p>
      </div>

      <label className="flex items-center gap-2 text-sm font-medium">
        <input
          type="checkbox"
          aria-label={t("showArchived")}
          checked={includeArchived}
          disabled={busy || conflict}
          onChange={(event) => {
            setIncludeArchived(event.target.checked);
            setSelectedId(null);
            setError(null);
          }}
        />
        {t("showArchived")}
      </label>

      <div className="grid gap-3 md:grid-cols-2">
        <label className="block text-sm font-medium">
          {t("source")}
          <select
            aria-label={t("source")}
            className="mt-1 w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
            value={sourceWorkflowId ?? ""}
            disabled
            onChange={() => undefined}
          >
            <option value="">{t("chooseInTable")}</option>
            {workflows.map((workflow) => (
              <option key={workflow.id} value={workflow.id}>
                {workflow.name} · v{workflow.version}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm font-medium">
          {t("templateName")}
          <input
            aria-label={t("templateName")}
            className="mt-1 w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
            value={templateName}
            disabled={busy || conflict}
            onChange={(event) => setTemplateName(event.target.value)}
          />
        </label>
      </div>
      <button
        type="button"
        className="rounded-btn border border-aurum-border bg-aurum-surface px-3 py-2 text-sm font-medium text-aurum-text-2 hover:bg-aurum-surface-3 disabled:opacity-50"
        disabled={busy || conflict || !sourceReady || !templateName.trim()}
        onClick={() =>
          void run(async () => {
            const saved = source.data?.data;
            if (!saved) return;
            const result = await capture.mutateAsync({
              sourceWorkflowId: saved.id,
              sourceWorkflowVersion: saved.version,
              name: templateName.trim(),
              description: saved.description ?? null,
            });
            setSelectedId(result.data.id);
            setTemplateName("");
          })
        }
      >
        {t("saveAsTemplate")}
      </button>

      <div className="grid gap-3 md:grid-cols-2">
        <label className="block text-sm font-medium">
          {t("template")}
          <select
            aria-label={t("template")}
            className="mt-1 w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
            value={effectiveSelectedId ?? ""}
            disabled={busy || conflict || templates.isLoading}
            onChange={(event) => {
              setSelectedId(event.target.value || null);
              setError(null);
              setCopyNotice(false);
            }}
          >
            <option value="">{t("chooseTemplate")}</option>
            {templates.data?.data.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · v{item.version}
                {item.status === "archived" ? t("archivedSuffix") : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm font-medium">
          {t("copyName")}
          <input
            aria-label={t("copyName")}
            className="mt-1 w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
            value={copyName}
            disabled={busy || conflict || selectedArchived}
            onChange={(event) => setCopyName(event.target.value)}
          />
        </label>
      </div>

      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className="rounded-btn border border-aurum-border bg-aurum-surface px-3 py-2 text-sm font-medium text-aurum-text-2 hover:bg-aurum-surface-3 disabled:opacity-50"
          disabled={selectedWriteDisabled || !sourceReady}
          onClick={() =>
            void run(async () => {
              const saved = source.data?.data;
              if (!selected || !saved) return;
              await replace.mutateAsync({
                id: selected.id,
                payload: {
                  sourceWorkflowId: saved.id,
                  sourceWorkflowVersion: saved.version,
                  version: selected.version,
                  name: selected.name,
                  description: selected.description,
                },
              });
            })
          }
        >
          {t("updateFromWorkflow")}
        </button>
        <button
          type="button"
          className="rounded-btn bg-aurum-accent px-3 py-2 text-sm font-semibold text-aurum-accent-ink disabled:opacity-50"
          disabled={selectedWriteDisabled || !copyName.trim()}
          onClick={() =>
            void run(async () => {
              if (!selected) return;
              const result = await instantiate.mutateAsync({
                id: selected.id,
                payload: { version: selected.version, name: copyName.trim() },
              });
              setCopyNotice(true);
              setCopyName("");
              onWorkflowCreated(result.data.workflow.id);
            })
          }
        >
          {t("createFromTemplate")}
        </button>
        <button
          type="button"
          className="rounded-btn border border-aurum-border bg-aurum-surface px-3 py-2 text-sm font-medium text-aurum-err-fg hover:bg-aurum-surface-3 disabled:opacity-50"
          disabled={selectedWriteDisabled}
          onClick={() =>
            void run(async () => {
              if (!selected) return;
              await archive.mutateAsync({
                id: selected.id,
                version: selected.version,
              });
              setArchivedIds((current) => new Set(current).add(selected.id));
            })
          }
        >
          {t("archiveTemplate")}
        </button>
      </div>

      {error ? <p className="text-sm text-aurum-err-fg">{error}</p> : null}
      {conflict ? (
        <button
          type="button"
          className="rounded-btn border border-aurum-border bg-aurum-surface px-3 py-2 text-sm font-medium text-aurum-text-2 hover:bg-aurum-surface-3"
          disabled={busy}
          onClick={() => void refreshAfterConflict()}
        >
          {t("refresh")}
        </button>
      ) : null}
      {copyNotice ? (
        <p className="rounded-input bg-c-amber-bg p-3 text-sm text-c-amber-fg">
          {t("copyNotice")}
        </p>
      ) : null}
    </section>
  );
}
