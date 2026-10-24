"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, GitBranch, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { GrantEditor } from "@/components/authorization/GrantEditor";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { WorkflowDiagnostics } from "@/components/workflows/WorkflowDiagnostics";
import { WorkflowExamples } from "@/components/workflows/WorkflowExamples";
import { WorkflowForm } from "@/components/workflows/WorkflowForm";
import { WorkflowTable } from "@/components/workflows/WorkflowTable";
import { WorkflowTemplatesPanel } from "@/components/workflows/WorkflowTemplatesPanel";
import { useAdminAccess } from "@/hooks/useAdminAccess";
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
  const t = useTranslations("access");
  const access = useAdminAccess();
  if (access.isLoading) return <div>{t("checking")}</div>;
  if (!access.allowed)
    return (
      <div className="rounded-card border border-aurum-border bg-aurum-err-bg p-6 text-aurum-err-fg">
        {access.error || t("adminRequired")}
      </div>
    );
  return <AdminWorkflowsContent canWrite={access.canWrite} />;
}

function AdminWorkflowsContent({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("workflows.page");
  const w = useTranslations("workflows.workspace");
  const g = useTranslations("workflows.guide");
  const examples = useTranslations("workflows.examples");
  const workflows = useWorkflows(true);
  const agents = useQuery({
    queryKey: ["agent-definitions"],
    queryFn: getAgentDefinitions,
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
    <main className="workflow-workspace min-w-0 space-y-6 text-aurum-text">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("title")}</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-aurum-text-2">
            {t("subtitle")}
          </p>
        </div>
        {canWrite && view !== "editor" ? (
          <button
            type="button"
            onClick={() => openEditor(null)}
            className="flex items-center gap-2 rounded-btn bg-aurum-accent px-4 py-2.5 text-sm font-semibold text-aurum-accent-ink"
          >
            <Plus size={17} />
            {w("create")}
          </button>
        ) : null}
      </header>
      <nav
        className="flex flex-wrap gap-2 border-b border-aurum-border pb-4"
        aria-label={t("title")}
      >
        {(["overview", "examples", "templates", "diagnostics"] as const)
          .filter((item) => canWrite || item !== "templates")
          .map((item) => (
            <button
              key={item}
              type="button"
              disabled={busy}
              aria-current={view === item ? "page" : undefined}
              onClick={() => switchView(item)}
              className={`${button} ${view === item ? "bg-aurum-surface-3" : "bg-aurum-surface"}`}
            >
              {w(item)}
            </button>
          ))}
      </nav>
      {view === "overview" ? (
        <>
          <section className="flex gap-4 rounded-card border border-aurum-border bg-aurum-surface p-5">
            <GitBranch className="mt-1 shrink-0 text-aurum-text-3" size={23} />
            <div>
              <h2 className="font-semibold">{w("howTitle")}</h2>
              <p className="mt-2 text-sm leading-6 text-aurum-text-2">
                {w("howText")}
              </p>
            </div>
          </section>
          {workflows.isLoading ? <p role="status">{t("loading")}</p> : null}
          {workflows.isError ? <p role="alert">{w("loadFailed")}</p> : null}
          {workflows.data?.data.length ? (
            <WorkflowTable
              workflows={workflows.data.data}
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
          ) : workflows.data ? (
            <section className="rounded-card border border-dashed border-aurum-border bg-aurum-surface p-8 text-center">
              <BookOpen className="mx-auto mb-4 text-aurum-text-3" size={30} />
              <h2 className="text-lg font-semibold">{w("emptyTitle")}</h2>
              <p className="mx-auto my-3 max-w-xl text-sm leading-6 text-aurum-text-2">
                {w("emptyHint")}
              </p>
              <button
                type="button"
                className={button}
                aria-label={w("browseExamples")}
                onClick={() => switchView("examples")}
              >
                {w("examples")}
              </button>
            </section>
          ) : null}
          {grantWorkflow ? renderGrants(grantWorkflow) : null}
          {archive.isError ? <p role="alert">{w("loadFailed")}</p> : null}
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
          <button
            type="button"
            className={button}
            disabled={busy}
            onClick={() => switchView("overview")}
          >
            {w("cancel")}
          </button>
          {editor.fromTemplate ? (
            <p className="rounded-input border border-aurum-border bg-aurum-surface-2 p-4 text-sm">
              {examples("templateCopyNotice")}
            </p>
          ) : null}
          {(!agents.data && agents.isLoading) ||
          (editor.id && !detail.data && detail.isLoading) ? (
            <p role="status">{w("loadingDetail")}</p>
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
                  <Link href="/agents/constructor" className="underline">
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
        <section className="space-y-4 rounded-card border border-aurum-border bg-aurum-surface p-5">
          <h2 className="text-lg font-semibold">{w("diagnostics")}</h2>
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
          {diagnostic.isError ? <p role="alert">{w("runFailed")}</p> : null}
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
    </main>
  );
}
