"use client";

import { useState } from "react";

import type { AgentDefinition, WorkflowDefinition, WorkflowDraft } from "@/lib/api";
import { WorkflowCanvas } from "@/components/workflows/WorkflowCanvas";
import {
  computeAutoLayout,
  computeValidity,
  emptyNode,
  isDuplicateKeys,
  needsAutoLayout,
} from "@/lib/workflow-graph";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

type Props = {
  agents: AgentDefinition[];
  workflow?: WorkflowDefinition | null;
  busy?: boolean;
  onValidate: (draft: WorkflowDraft) => Promise<void>;
  onSubmit: (draft: WorkflowDraft) => Promise<void>;
};

const emptyDraft = (): WorkflowDraft => ({
  name: "",
  description: "",
  supervisorAgentDefinitionId: "",
  maxParallelism: 1,
  autoRoutable: false,
  nodes: [emptyNode(1)],
  edges: [],
});

const editableDraft = (
  workflow: WorkflowDefinition | null | undefined
): WorkflowDraft => {
  if (!workflow) return emptyDraft();
  return {
    name: workflow.name,
    description: workflow.description ?? "",
    supervisorAgentDefinitionId: workflow.supervisorAgentDefinitionId,
    maxParallelism: workflow.maxParallelism,
    autoRoutable: workflow.autoRoutable ?? false,
    nodes: needsAutoLayout(workflow.nodes)
      ? computeAutoLayout(workflow.nodes, workflow.edges)
      : workflow.nodes,
    edges: workflow.edges,
  };
};

export function WorkflowForm({
  agents,
  workflow,
  busy = false,
  onValidate,
  onSubmit,
}: Props) {
  const t = useTranslations("workflows.form");
  const errorMessage = useErrorMessage();
  const [draft, setDraft] = useState<WorkflowDraft>(() => editableDraft(workflow));
  const [validated, setValidated] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const duplicateKeys = isDuplicateKeys(draft.nodes);
  const invalid = computeValidity(draft);

  const change = (next: WorkflowDraft) => {
    setDraft(next);
    setValidated(false);
    setError(null);
  };

  const validate = async () => {
    try {
      await onValidate(draft);
      setValidated(true);
      setError(null);
    } catch (cause) {
      setValidated(false);
      setError(cause instanceof Error ? errorMessage(cause) : t("validateFailed"));
    }
  };

  const submit = async () => {
    try {
      await onSubmit(draft);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? errorMessage(cause) : t("saveFailed"));
    }
  };

  return (
    <form
      className="space-y-5 rounded-md border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h2 className="text-lg font-semibold">
        {workflow ? t("editTitle") : t("newTitle")}
      </h2>
      <label className="block text-sm font-medium">
        {t("name")}
        <input
          aria-label={t("nameAria")}
          className="mt-1 w-full rounded-md border px-3 py-2"
          value={draft.name}
          onChange={(event) => change({ ...draft, name: event.target.value })}
        />
      </label>
      <label className="block text-sm font-medium">
        {t("description")}
        <textarea
          aria-label={t("descriptionAria")}
          className="mt-1 w-full rounded-md border px-3 py-2"
          value={draft.description ?? ""}
          onChange={(event) =>
            change({ ...draft, description: event.target.value })
          }
        />
      </label>
      <label className="block text-sm font-medium">
        Supervisor
        <select
          aria-label="Supervisor"
          className="mt-1 w-full rounded-md border px-3 py-2"
          value={draft.supervisorAgentDefinitionId}
          onChange={(event) =>
            change({ ...draft, supervisorAgentDefinitionId: event.target.value })
          }
        >
          <option value="">{t("chooseAgent")}</option>
          {agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name}
            </option>
          ))}
        </select>
      </label>
      <label className="block text-sm font-medium">
        {t("parallelism")}
        <select
          aria-label={t("parallelism")}
          className="ml-3 rounded-md border px-3 py-2"
          value={draft.maxParallelism}
          onChange={(event) =>
            change({ ...draft, maxParallelism: Number(event.target.value) })
          }
        >
          {[1, 2, 3, 4].map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm font-medium">
        <input
          type="checkbox"
          aria-label={t("autoRouting")}
          checked={draft.autoRoutable ?? false}
          onChange={(event) =>
            change({ ...draft, autoRoutable: event.target.checked })
          }
        />
        {t("autoRoutingHint")}
      </label>

      <WorkflowCanvas
        agents={agents}
        nodes={draft.nodes}
        edges={draft.edges}
        onChange={(nodes, edges) => change({ ...draft, nodes, edges })}
      />

      {duplicateKeys ? <p className="text-sm text-red-600">{t("duplicateKeys")}</p> : null}
      {error ? <p className="text-sm text-red-600">{error}</p> : null}
      <div className="flex gap-3">
        <button type="button" disabled={invalid || busy} onClick={() => void validate()} className="rounded-md border px-4 py-2 disabled:opacity-50">
          {t("validate")}
        </button>
        <button type="submit" disabled={invalid || !validated || busy} className="rounded-md bg-primary-600 px-4 py-2 text-white disabled:opacity-50">
          {t("save")}
        </button>
      </div>
    </form>
  );
}
