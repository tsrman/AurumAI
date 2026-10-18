"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { GrantEditor } from "@/components/authorization/GrantEditor";
import { WorkflowDiagnostics } from "@/components/workflows/WorkflowDiagnostics";
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
import { getAgentDefinitions } from "@/lib/api";
import type { WorkflowDraft, WorkflowListItem } from "@/lib/api";
import { useTranslations } from "next-intl";

async function workflowError(cause: unknown): Promise<Error> {
  if (typeof cause === "object" && cause !== null && "response" in cause) {
    const response = (cause as { response?: Response }).response;
    const body = await response?.clone().json().catch(() => null) as
      | { detail?: string | { code?: string }; error?: { code?: string } }
      | null;
    const code =
      (typeof body?.detail === "object" ? body.detail.code : body?.detail) ??
      body?.error?.code;
    if (code) return new Error(code);
  }
  return cause instanceof Error ? cause : new Error("workflow_error");
}

export default function AdminWorkflowsPage() {
  const accessT = useTranslations("access");
  const access = useAdminAccess();

  if (access.isLoading) return <div>{accessT("checking")}</div>;
  if (!access.allowed) {
    return <div className="rounded-md border border-red-200 bg-red-50 p-6 text-red-700">{access.error || accessT("adminRequired")}</div>;
  }

  return <AdminWorkflowsContent canWrite={access.canWrite} />;
}

function AdminWorkflowsContent({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("workflows.page");
  const workflows = useWorkflows(true);
  const agents = useQuery({ queryKey: ["agent-definitions"], queryFn: getAgentDefinitions });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [sourceWorkflowId, setSourceWorkflowId] = useState<string | null>(null);
  const [grantWorkflow, setGrantWorkflow] = useState<WorkflowListItem | null>(null);
  const [diagnosticRunId, setDiagnosticRunId] = useState("");
  const detail = useWorkflow(editingId);
  const diagnostic = useWorkflowDiagnostic(diagnosticRunId || null);
  const create = useCreateWorkflow();
  const replace = useReplaceWorkflow();
  const archive = useArchiveWorkflow();
  const validate = useValidateWorkflow();

  const submit = async (draft: WorkflowDraft) => {
    try {
      if (detail.data?.data) {
        await replace.mutateAsync({
          id: detail.data.data.id,
          version: detail.data.data.version,
          draft,
        });
        setEditingId(null);
      } else {
        await create.mutateAsync(draft);
      }
    } catch (cause) {
      throw await workflowError(cause);
    }
  };

  return (
    <main className="space-y-6">
      <header><h1 className="text-2xl font-semibold">{t("title")}</h1><p className="text-sm text-gray-600">{t("subtitle")}</p></header>
      {workflows.isLoading ? <p>{t("loading")}</p> : null}
      {workflows.data ? (
        <WorkflowTable
          workflows={workflows.data.data}
          onEdit={setEditingId}
          onArchive={(id) => archive.mutate(id)}
          onGrants={setGrantWorkflow}
          onSaveTemplate={canWrite ? setSourceWorkflowId : undefined}
        />
      ) : null}
      {canWrite && workflows.data ? (
        <WorkflowTemplatesPanel
          workflows={workflows.data.data}
          sourceWorkflowId={sourceWorkflowId}
          onWorkflowCreated={(id) => {
            setEditingId(id);
            void agents.refetch();
          }}
        />
      ) : null}
      {canWrite ? (
        <WorkflowForm
          key={detail.data?.data ? `${detail.data.data.id}:${detail.data.data.version}` : "new"}
          agents={agents.data?.data ?? []}
          workflow={detail.data?.data ?? null}
          busy={create.isPending || replace.isPending || validate.isPending}
          onValidate={async (draft) => {
            try {
              await validate.mutateAsync(draft);
            } catch (cause) {
              throw await workflowError(cause);
            }
          }}
          onSubmit={submit}
        />
      ) : null}
      {grantWorkflow ? (
        <GrantEditor
          resourceType="workflow"
          resourceId={grantWorkflow.id}
          resourceLabel={grantWorkflow.name}
        />
      ) : null}
      <section className="space-y-2">
        <label className="block text-sm font-medium">Run ID
          <input aria-label="Run ID" className="ml-2 rounded-md border px-3 py-2" value={diagnosticRunId} onChange={(event) => setDiagnosticRunId(event.target.value)} />
        </label>
        {diagnostic.data ? <WorkflowDiagnostics diagnostic={diagnostic.data.data} /> : null}
      </section>
    </main>
  );
}
