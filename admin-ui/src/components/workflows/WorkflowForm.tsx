"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowLeft, ArrowRight, Check, CheckCircle2 } from "lucide-react";
import { useTranslations } from "next-intl";
import type {
  AgentDefinition,
  WorkflowDefinition,
  WorkflowDraft,
} from "@/lib/api";
import { WorkflowCanvas } from "@/components/workflows/WorkflowCanvas";
import {
  computeAutoLayout,
  computeValidity,
  emptyNode,
  isDuplicateKeys,
  needsAutoLayout,
} from "@/lib/workflow-graph";
import { useErrorMessage } from "@/lib/errors";

const steps = ["task", "agents", "steps", "check", "launch"] as const;
const field =
  "mt-1 w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2.5 text-sm text-aurum-text";
const secondary =
  "rounded-btn border border-aurum-border px-4 py-2 text-sm font-medium hover:bg-aurum-surface-2 disabled:opacity-50";
const primary =
  "rounded-btn bg-aurum-accent px-4 py-2 text-sm font-semibold text-aurum-accent-ink disabled:opacity-50";

type Props = {
  agents: AgentDefinition[];
  workflow?: WorkflowDefinition | null;
  initialDraft?: WorkflowDraft;
  busy?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  onValidate: (draft: WorkflowDraft) => Promise<void>;
  onSubmit: (
    draft: WorkflowDraft,
    version?: number,
  ) => Promise<WorkflowDefinition | void>;
  launchContent?: React.ReactNode;
};

function editableDraft(workflow?: WorkflowDefinition | null): WorkflowDraft {
  if (!workflow)
    return {
      name: "",
      description: "",
      supervisorAgentDefinitionId: "",
      maxParallelism: 1,
      autoRoutable: false,
      nodes: [emptyNode(1)],
      edges: [],
    };
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
}

export function WorkflowForm({
  agents,
  workflow,
  initialDraft,
  busy = false,
  onDirtyChange,
  onValidate,
  onSubmit,
  launchContent,
}: Props) {
  const t = useTranslations("workflows.form");
  const g = useTranslations("workflows.guide");
  const c = useTranslations("workflows.canvas");
  const errorMessage = useErrorMessage();
  const [draft, setDraft] = useState<WorkflowDraft>(
    () => initialDraft ?? editableDraft(workflow),
  );
  const [step, setStep] = useState(0);
  const [baseVersion, setBaseVersion] = useState(workflow?.version);
  const [validated, setValidated] = useState(false);
  const [saved, setSaved] = useState(Boolean(workflow));
  const [error, setError] = useState<string | null>(null);
  const [localBusy, setLocalBusy] = useState(false);
  const pending = busy || localBusy;
  const invalid =
    computeValidity(draft) ||
    !agents.some((agent) => agent.id === draft.supervisorAgentDefinitionId) ||
    draft.nodes.some(
      (node) => !agents.some((agent) => agent.id === node.agentDefinitionId),
    );
  const missing = [
    !draft.name.trim() ? g("missingName") : null,
    !agents.some((agent) => agent.id === draft.supervisorAgentDefinitionId)
      ? g("missingSupervisor")
      : null,
    draft.nodes.some(
      (node) =>
        !node.displayName.trim() ||
        !node.instruction.trim() ||
        !agents.some((agent) => agent.id === node.agentDefinitionId),
    )
      ? g("missingNodes")
      : null,
    computeValidity({
      ...draft,
      name: draft.name || "_",
      supervisorAgentDefinitionId: draft.supervisorAgentDefinitionId || "_",
      nodes: draft.nodes.map((node) => ({
        ...node,
        displayName: node.displayName || "_",
        instruction: node.instruction || "_",
        agentDefinitionId: node.agentDefinitionId || "_",
      })),
    })
      ? g("missingGraph")
      : null,
  ].filter((value): value is string => value !== null);

  const change = (next: WorkflowDraft) => {
    setDraft(next);
    setValidated(false);
    setSaved(false);
    setError(null);
    onDirtyChange?.(true);
  };
  const validate = async () => {
    if (pending || invalid) return;
    setLocalBusy(true);
    try {
      await onValidate(draft);
      setValidated(true);
      setError(null);
    } catch (cause) {
      setValidated(false);
      setError(
        cause instanceof Error ? errorMessage(cause) : t("validateFailed"),
      );
    } finally {
      setLocalBusy(false);
    }
  };
  const submit = async () => {
    if (step !== 3 || pending || invalid || !validated) return;
    setLocalBusy(true);
    try {
      const result =
        baseVersion === undefined
          ? await onSubmit(draft)
          : await onSubmit(draft, baseVersion);
      if (result) setBaseVersion(result.version);
      setSaved(true);
      setError(null);
      onDirtyChange?.(false);
      setStep(4);
    } catch (cause) {
      setError(cause instanceof Error ? errorMessage(cause) : t("saveFailed"));
    } finally {
      setLocalBusy(false);
    }
  };
  const agentOptions = (
    <>
      <option value="">{t("chooseAgent")}</option>
      {agents.map((agent) => (
        <option key={agent.id} value={agent.id}>
          {agent.name}
        </option>
      ))}
    </>
  );

  // The supervisor plans and synthesizes; Hermes Agent runs nodes only.
  const supervisorOptions = (
    <>
      <option value="">{t("chooseAgent")}</option>
      {agents
        .filter((agent) => agent.kind !== "hermes")
        .map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.name}
          </option>
        ))}
    </>
  );

  return (
    <form
      className="min-w-0 space-y-6"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h2 className="text-xl font-semibold">
        {workflow ? t("editTitle") : t("newTitle")}
      </h2>
      <nav
        aria-label={g("steps")}
        className="grid grid-cols-2 gap-2 sm:grid-cols-5"
      >
        {steps.map((name, index) => (
          <button
            key={name}
            type="button"
            disabled={pending}
            aria-current={step === index ? "step" : undefined}
            onClick={() => setStep(index)}
            className={`rounded-btn border px-3 py-3 text-left text-sm font-medium ${step === index ? "border-aurum-accent bg-aurum-accent-soft" : "border-aurum-border bg-aurum-surface"}`}
          >
            {g("stepLabel", { number: index + 1, name: g(name) })}
          </button>
        ))}
      </nav>
      <section
        className="min-w-0 space-y-5 rounded-card border border-aurum-border bg-aurum-surface p-4 sm:p-6"
        aria-label={g(steps[step])}
      >
        {step === 0 ? (
          <>
            <label className="block text-sm font-medium">
              {t("name")}
              <input
                aria-label={t("nameAria")}
                className={field}
                value={draft.name}
                disabled={pending}
                onChange={(event) =>
                  change({ ...draft, name: event.target.value })
                }
              />
            </label>
            <label className="block text-sm font-medium">
              {t("description")}
              <textarea
                aria-label={t("descriptionAria")}
                className={field}
                rows={4}
                value={draft.description ?? ""}
                disabled={pending}
                onChange={(event) =>
                  change({ ...draft, description: event.target.value })
                }
              />
            </label>
          </>
        ) : null}
        {step === 1 ? (
          <>
            <p className="text-sm leading-6 text-aurum-text-2">
              {g("agentsHint")}
            </p>
            {agents.length === 0 ? (
              <p role="status">
                {g("noAgents")}{" "}
                <Link className="underline" href="/admin/agents/new">
                  {g("agents")}
                </Link>
              </p>
            ) : null}
            <label className="block text-sm font-medium">
              {g("supervisor")}
              <select
                aria-label={g("supervisor")}
                className={field}
                value={draft.supervisorAgentDefinitionId}
                disabled={pending}
                onChange={(event) =>
                  change({
                    ...draft,
                    supervisorAgentDefinitionId: event.target.value,
                  })
                }
              >
                {supervisorOptions}
              </select>
            </label>
            <p className="text-sm text-aurum-text-2">{g("supervisorHint")}</p>
            <button
              className={secondary}
              type="button"
              disabled={!draft.supervisorAgentDefinitionId || pending}
              onClick={() =>
                change({
                  ...draft,
                  nodes: draft.nodes.map((node) => ({
                    ...node,
                    agentDefinitionId: draft.supervisorAgentDefinitionId,
                  })),
                })
              }
            >
              {g("allAgents")}
            </button>
            <div className="grid gap-4 sm:grid-cols-2">
              {draft.nodes.map((node, index) => (
                <label key={index} className="block text-sm font-medium">
                  {g("nodeAgent", { name: node.displayName || node.nodeKey })}
                  <select
                    aria-label={c("nodeAgent", { index: index + 1 })}
                    className={field}
                    disabled={pending}
                    value={node.agentDefinitionId}
                    onChange={(event) =>
                      change({
                        ...draft,
                        nodes: draft.nodes.map((item, i) =>
                          i === index
                            ? { ...item, agentDefinitionId: event.target.value }
                            : item,
                        ),
                      })
                    }
                  >
                    {agentOptions}
                  </select>
                </label>
              ))}
            </div>
          </>
        ) : null}
        {step === 2 ? (
          <>
            <p className="text-sm leading-6 text-aurum-text-2">
              {g("graphHint")}
            </p>
            <WorkflowCanvas
              agents={agents}
              nodes={draft.nodes}
              edges={draft.edges}
              onChange={(nodes, edges) => change({ ...draft, nodes, edges })}
            />
            <div className="space-y-2 rounded-input bg-aurum-surface-2 p-4 text-sm leading-6 text-aurum-text-2">
              <p>{g("edgeHint")}</p>
              <p>{g("confirmationHint")}</p>
            </div>
          </>
        ) : null}
        {step === 3 ? (
          <>
            <div className="flex items-center gap-2 font-medium">
              <CheckCircle2 size={19} />
              {validated ? g("validated") : g("ready")}
            </div>
            <p className="text-sm text-aurum-text-2">{g("checkHint")}</p>
            {missing.length ? (
              <ul className="list-disc space-y-2 pl-5 text-sm text-aurum-err-fg">
                {missing.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            ) : (
              <p className="text-sm">
                {draft.name} · {g("nodeCount", { count: draft.nodes.length })}
              </p>
            )}
            {isDuplicateKeys(draft.nodes) ? (
              <p className="text-sm text-aurum-err-fg">{t("duplicateKeys")}</p>
            ) : null}
            <div className="flex flex-wrap gap-3">
              <button
                type="button"
                className={secondary}
                disabled={invalid || pending}
                onClick={() => void validate()}
              >
                {t("validate")}
              </button>
              <button
                type="submit"
                className={primary}
                disabled={invalid || !validated || pending}
              >
                {t("save")}
              </button>
            </div>
          </>
        ) : null}
        {step === 4 ? (
          <div className="space-y-4">
            <p className="flex items-center gap-2 font-medium">
              <Check size={20} />
              {saved ? g("launchHint") : g("saveFirst")}
            </p>
            {saved ? launchContent : null}
          </div>
        ) : null}
        {step < 4 ? (
          <details className="rounded-input border border-aurum-border p-4">
            <summary className="cursor-pointer text-sm font-medium">
              {g("advanced")}
            </summary>
            <div className="mt-4 space-y-4">
              <label className="block text-sm">
                {t("parallelism")}
                <select
                  aria-label={t("parallelism")}
                  className={field}
                  value={draft.maxParallelism}
                  disabled={pending}
                  onChange={(event) =>
                    change({
                      ...draft,
                      maxParallelism: Number(event.target.value),
                    })
                  }
                >
                  {[1, 2, 3, 4].map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  aria-label={t("autoRouting")}
                  checked={draft.autoRoutable ?? false}
                  disabled={pending}
                  onChange={(event) =>
                    change({ ...draft, autoRoutable: event.target.checked })
                  }
                />
                {t("autoRoutingHint")}
              </label>
            </div>
          </details>
        ) : null}
        {error ? (
          <p
            role="alert"
            className="rounded-input bg-aurum-err-bg p-3 text-sm text-aurum-err-fg"
          >
            {error}
          </p>
        ) : null}
      </section>
      <div className="flex justify-between gap-3">
        <button
          type="button"
          className={`${secondary} flex items-center gap-2`}
          disabled={step === 0 || pending}
          onClick={() => setStep(step - 1)}
        >
          <ArrowLeft size={15} />
          {g("back")}
        </button>
        {step < 4 ? (
          <button
            type="button"
            className={`${primary} flex items-center gap-2`}
            disabled={pending}
            onClick={() => setStep(step + 1)}
          >
            {g("next")}
            <ArrowRight size={15} />
          </button>
        ) : null}
      </div>
    </form>
  );
}
