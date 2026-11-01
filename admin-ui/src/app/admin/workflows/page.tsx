"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { GrantEditor } from "@/components/authorization/GrantEditor";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { WorkflowDiagnostics } from "@/components/workflows/WorkflowDiagnostics";
import { WorkflowExamples } from "@/components/workflows/WorkflowExamples";
import { WorkflowForm } from "@/components/workflows/WorkflowForm";
import { WorkflowTable } from "@/components/workflows/WorkflowTable";
import { WorkflowTemplatesPanel } from "@/components/workflows/WorkflowTemplatesPanel";
import { AccessDialog } from "@/components/authorization/AccessDialog";
import { Button } from "@/components/primitives/Button";
import { Toggle } from "@/components/primitives/Toggle";
import { AdminGate, ButtonTabs, EmptyState, Notice, PageBody, PageHeader } from "@/components/ui";
import {
  useArchiveWorkflow,
  useCreateWorkflow,
  useReplaceWorkflow,
  useValidateWorkflow,
  useWorkflow,
  useWorkflowDiagnostic,
  useWorkflows,
} from "@/hooks/useWorkflows";
import {
  getAgentDefinitions,
  type WorkflowDraft,
  type WorkflowListItem,
} from "@/lib/api";
import { getRuntimeConfig } from "@/lib/runtime-config";
import type { WorkflowExampleId } from "@/lib/workflow-examples";

async function workflowError(cause: unknown): Promise<Error> {
  if (typeof cause === "object" && cause !== null && "response" in cause) {
    const response = (cause as { response?: Response }).response;
    const body = (await response
      ?.clone()
      .json()
      .catch(() => null)) as {
      detail?: string | { code?: string };
      error?: { code?: string };
    } | null;
    const code =
      (typeof body?.detail === "object" ? body.detail.code : body?.detail) ??
      body?.error?.code;
    if (code) return new Error(code);
  }
  return cause instanceof Error ? cause : new Error("workflow_error");
}

type View = "overview" | "examples" | "templates" | "diagnostics" | "editor";
type Editor = {
  id: string | null;
  key: number;
  draft?: WorkflowDraft;
  exampleId?: WorkflowExampleId;
  fromTemplate?: boolean;
};
const button =
  "rounded-btn border border-aurum-border px-4 py-2 text-sm font-medium hover:bg-aurum-surface-2 disabled:opacity-50";

export default function AdminWorkflowsPage() {
  return (
    <AdminGate>
      {({ canWrite }) => <AdminWorkflowsContent canWrite={canWrite} />}
    </AdminGate>
  );
}

function AdminWorkflowsContent({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("workflows.page");
  const w = useTranslations("workflows.workspace");
  const g = useTranslations("workflows.guide");
  const examples = useTranslations("workflows.examples");
  // Everything is fetched (the templates panel picks sources among all of it);
  // the overview hides archived workflows unless asked.
  const workflows = useWorkflows(true);
  const [showArchived, setShowArchived] = useState(false);
  const visibleWorkflows = (workflows.data?.data ?? []).filter((item) => showArchived || item.status !== "archived");
  const agents = useQuery({
    queryKey: ["agent-definitions"],
    queryFn: () => getAgentDefinitions(),
  });
  const [view, setView] = useState<View>("overview");
  const [editor, setEditor] = useState<Editor | null>(null);
  const serial = useRef(0);
  const allowNavigation = useRef(false);
  const [dirty, setDirty] = useState(false);
  const [transition, setTransition] = useState<{ action: () => void } | null>(
    null,
  );
  const [sourceWorkflowId, setSourceWorkflowId] = useState<string | null>(null);
  const [grantWorkflow, setGrantWorkflow] = useState<WorkflowListItem | null>(
    null,
  );
  const [savedWorkflow, setSavedWorkflow] = useState<WorkflowListItem | null>(
    null,
  );
  const [archiveTarget, setArchiveTarget] = useState<string | null>(null);
  const [diagnosticInput, setDiagnosticInput] = useState("");
  const [diagnosticRunId, setDiagnosticRunId] = useState("");
  const [copied, setCopied] = useState<"idle" | "copied" | "failed">("idle");
  const detail = useWorkflow(editor?.id ?? null);
  const diagnostic = useWorkflowDiagnostic(diagnosticRunId || null);
  const create = useCreateWorkflow();
  const replace = useReplaceWorkflow();
  const archive = useArchiveWorkflow();
  const validate = useValidateWorkflow();
  const busy = create.isPending || replace.isPending || validate.isPending;

  useEffect(() => {
    if (!dirty) return;
    allowNavigation.current = false;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (allowNavigation.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    const followLink = (event: MouseEvent) => {
      if (
        event.button !== 0 ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const link =
        event.target instanceof Element ? event.target.closest("a") : null;
      if (
        !(link instanceof HTMLAnchorElement) ||
        link.target === "_blank" ||
        link.hasAttribute("download")
      )
        return;
      const destination = new URL(link.href, window.location.href);
      if (
        !["http:", "https:"].includes(destination.protocol) ||
        destination.href === window.location.href
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      setTransition({
        action: () => {
          allowNavigation.current = true;
          window.location.assign(destination.href);
        },
      });
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", followLink, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", followLink, true);
    };
  }, [dirty]);

  const navigate = (action: () => void) => {
    if (busy) return;
    if (dirty) setTransition({ action });
    else action();
  };
  const switchView = (next: View) =>
    navigate(() => {
      setDirty(false);
      setView(next);
      setEditor(null);
      setSavedWorkflow(null);
      setGrantWorkflow(null);
    });
  const openEditor = (
    id: string | null,
    draft?: WorkflowDraft,
    exampleId?: WorkflowExampleId,
    fromTemplate = false,
  ) =>
    navigate(() => {
      setEditor({ id, key: ++serial.current, draft, exampleId, fromTemplate });
      setDirty(Boolean(draft));
      setView("editor");
      setSavedWorkflow(null);
      setCopied("idle");
    });
  const submit = async (draft: WorkflowDraft, version?: number) => {
    try {
      const id = savedWorkflow?.id ?? editor?.id;
      const result =
        id && version !== undefined
          ? await replace.mutateAsync({ id, version, draft })
          : await create.mutateAsync(draft);
      setSavedWorkflow(result.data);
      return result.data;
    } catch (cause) {
      throw await workflowError(cause);
    }
  };
  const sample = editor?.exampleId
    ? examples(`${editor.exampleId}.prompt`)
    : null;
  const renderGrants = (workflow: WorkflowListItem) => (
    <GrantEditor
      key={workflow.id}
      resourceType="workflow"
      resourceId={workflow.id}
      resourceLabel={workflow.name}
    />
  );

  return (
    <div className="workflow-workspace flex min-w-0 flex-1 flex-col text-aurum-text">
      <PageHeader
        title={t("title")}
        description={t("subtitle")}
        actions={
          canWrite && view !== "editor" ? (
            <Button onClick={() => openEditor(null)}>
              <Plus size={17} aria-hidden="true" />
              {w("create")}
            </Button>
          ) : view === "editor" ? (
            <Button variant="secondary" disabled={busy} onClick={() => switchView("overview")}>
              <ArrowLeft size={16} aria-hidden="true" />
              {w("cancel")}
            </Button>
          ) : null
        }
        tabs={
          // Stays visible in the editor: leaving through a tab goes through
          // the same unsaved-changes confirmation as any other navigation.
          <ButtonTabs<View>
            label={t("title")}
            value={view}
            onChange={(item) => switchView(item)}
            tabs={(["overview", "examples", "templates", "diagnostics"] as const)
              .filter((item) => canWrite || item !== "templates")
              .map((item) => ({ value: item, label: w(item), disabled: busy }))}
          />
        }
      />
      <PageBody>
      {view === "overview" ? (
        <>
          <Notice title={w("howTitle")}>{w("howText")}</Notice>
          {workflows.isLoading ? (
            <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
          ) : null}
          {workflows.isError ? <Notice tone="error">{w("loadFailed")}</Notice> : null}
          {workflows.data?.data.length ? (
            <label className="flex items-center gap-2 text-sm text-aurum-text-2">
              <Toggle checked={showArchived} onChange={setShowArchived} aria-label={w("showArchived")} />
              {w("showArchived")}
            </label>
          ) : null}
          {visibleWorkflows.length ? (
            <WorkflowTable
              workflows={visibleWorkflows}
              canWrite={canWrite}
              onEdit={(id) => openEditor(id)}
              onArchive={setArchiveTarget}
              onGrants={setGrantWorkflow}
              onSaveTemplate={
                canWrite
                  ? (id) => {
                      setSourceWorkflowId(id);
                      setView("templates");
                    }
                  : undefined
              }
            />
          ) : workflows.data && workflows.data.data.length === 0 ? (
            <EmptyState
              icon={<BookOpen size={30} aria-hidden="true" />}
              title={w("emptyTitle")}
              description={w("emptyHint")}
              action={
                <Button
                  variant="secondary"
                  aria-label={w("browseExamples")}
                  onClick={() => switchView("examples")}
                >
                  {w("examples")}
                </Button>
              }
            />
          ) : null}
          {archive.isError ? <Notice tone="error">{w("loadFailed")}</Notice> : null}
        </>
      ) : null}
      {view === "examples" ? (
        <WorkflowExamples
          canWrite={canWrite}
          onUse={(draft, id) => openEditor(null, draft, id)}
        />
      ) : null}
      {view === "templates" && canWrite && workflows.data ? (
        <WorkflowTemplatesPanel
          workflows={workflows.data.data}
          sourceWorkflowId={sourceWorkflowId}
          onWorkflowCreated={(id) => {
            openEditor(id, undefined, undefined, true);
            void agents.refetch();
          }}
        />
      ) : null}
      {view === "editor" && editor && canWrite ? (
        <>
          {editor.fromTemplate ? (
            <p className="rounded-input border border-aurum-border bg-aurum-surface-2 p-4 text-sm">
              {examples("templateCopyNotice")}
            </p>
          ) : null}
          {(!agents.data && agents.isLoading) ||
          (editor.id && !detail.data && detail.isLoading) ? (
            <p role="status" className="text-sm text-aurum-text-3">{w("loadingDetail")}</p>
          ) : (!agents.data && agents.isError) ||
            (editor.id && !detail.data?.data) ? (
            <div role="alert" className="space-y-3">
              <p>{w("loadFailed")}</p>
              <button
                className={button}
                type="button"
                onClick={() => {
                  void agents.refetch();
                  if (editor.id) void detail.refetch();
                }}
              >
                {w("retry")}
              </button>
            </div>
          ) : (
            <>
              {agents.isError || (editor.id && detail.isError) ? (
                <p
                  role="alert"
                  className="rounded-input bg-aurum-err-bg p-3 text-sm text-aurum-err-fg"
                >
                  {w("refreshFailed")}
                </p>
              ) : null}
              {!agents.data?.data.length ? (
                <p className="rounded-input bg-aurum-surface-3 p-4 text-sm">
                  {w("agentsMissing")}{" "}
                  <Link href="/admin/agents/new" className="underline">
                    {w("agentsLink")}
                  </Link>
                </p>
              ) : null}
              <WorkflowForm
                key={editor.key}
                agents={agents.data?.data ?? []}
                workflow={editor.id ? detail.data?.data : null}
                initialDraft={editor.draft}
                onDirtyChange={setDirty}
                busy={busy}
                onValidate={async (draft) => {
                  try {
                    await validate.mutateAsync(draft);
                  } catch (cause) {
                    throw await workflowError(cause);
                  }
                }}
                onSubmit={submit}
                launchContent={
                  <div className="space-y-5">
                    <p className="text-sm text-aurum-text-2">
                      {w("accessHint")}
                    </p>
                    {savedWorkflow
                      ? renderGrants(savedWorkflow)
                      : editor.id && detail.data?.data
                        ? renderGrants(detail.data.data)
                        : null}
                    {sample ? (
                      <section className="space-y-3">
                        <h3 className="font-medium">{g("sampleInput")}</h3>
                        <blockquote className="whitespace-pre-wrap rounded-input bg-aurum-surface-2 p-4 text-sm leading-6">
                          {sample}
                        </blockquote>
                        <button
                          type="button"
                          className={button}
                          onClick={async () => {
                            try {
                              await navigator.clipboard.writeText(sample);
                              setCopied("copied");
                            } catch {
                              setCopied("failed");
                            }
                          }}
                        >
                          {g("copy")}
                        </button>
                        {copied !== "idle" ? (
                          <p role="status" className="text-sm">
                            {g(copied === "copied" ? "copied" : "copyFailed")}
                          </p>
                        ) : null}
                      </section>
                    ) : null}
                    <a
                      href={getRuntimeConfig().appUrl}
                      className="inline-block rounded-btn bg-aurum-accent px-4 py-2 text-sm font-semibold text-aurum-accent-ink"
                    >
                      {w("chat")}
                    </a>
                  </div>
                }
              />
            </>
          )}
        </>
      ) : null}
      {view === "diagnostics" ? (
        <section className="space-y-4 rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-card">
          <h2 className="text-base font-semibold">{w("diagnostics")}</h2>
          <p className="text-sm text-aurum-text-2">{w("runHint")}</p>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const runId = diagnosticInput.trim();
              if (runId === diagnosticRunId) void diagnostic.refetch();
              else setDiagnosticRunId(runId);
            }}
            className="flex flex-wrap items-end gap-3"
          >
            <label className="block min-w-0 flex-1 text-sm font-medium">
              {w("runId")}
              <input
                required
                pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                aria-label={w("runId")}
                className="mt-1 w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2"
                value={diagnosticInput}
                onChange={(event) => setDiagnosticInput(event.target.value)}
              />
            </label>
            <button className={button} type="submit">
              {w("diagnostics")}
            </button>
          </form>
          {diagnosticRunId && diagnostic.isLoading ? (
            <p role="status">{w("runLoading")}</p>
          ) : null}
          {diagnostic.isError ? <Notice tone="error">{w("runFailed")}</Notice> : null}
          {diagnostic.data ? (
            <WorkflowDiagnostics diagnostic={diagnostic.data.data} />
          ) : null}
        </section>
      ) : null}
      <ConfirmDialog
        open={transition !== null}
        title={w("discardTitle")}
        description={w("discardHint")}
        confirmLabel={w("discard")}
        cancelLabel={w("keep")}
        destructive
        onCancel={() => setTransition(null)}
        onConfirm={() => {
          const action = transition?.action;
          setTransition(null);
          setDirty(false);
          action?.();
        }}
      />
      <ConfirmDialog
        open={archiveTarget !== null}
        title={w("archiveTitle")}
        description={w("archiveHint")}
        confirmLabel={w("archiveConfirm")}
        destructive
        onCancel={() => setArchiveTarget(null)}
        onConfirm={() => {
          if (archiveTarget) archive.mutate(archiveTarget);
          setArchiveTarget(null);
        }}
      />
      <AccessDialog
        target={
          grantWorkflow
            ? { resourceType: "workflow", resourceId: grantWorkflow.id, label: grantWorkflow.name }
            : null
        }
        onClose={() => setGrantWorkflow(null)}
      />
      </PageBody>
    </div>
  );
}
