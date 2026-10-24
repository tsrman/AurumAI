import type { WorkflowDraft } from "@/lib/api";
import { computeAutoLayout } from "@/lib/workflow-graph";

export const workflowExampleIds = ["support", "campaign", "incident"] as const;
export type WorkflowExampleId = (typeof workflowExampleIds)[number];

const nodeKeys = {
  support: ["triage", "reply", "review"],
  campaign: ["audience", "messages", "campaign"],
  incident: ["evidence", "hypotheses", "runbook"],
} as const;

export function workflowExampleDraft(
  id: WorkflowExampleId,
  text: (key: string) => string,
): WorkflowDraft {
  const keys = nodeKeys[id];
  const edges = (
    id === "campaign"
      ? [
          [keys[0], keys[2]],
          [keys[1], keys[2]],
        ]
      : [
          [keys[0], keys[1]],
          [keys[1], keys[2]],
        ]
  ).map(([sourceNodeKey, targetNodeKey]) => ({
    sourceNodeKey,
    targetNodeKey,
    condition: "on_success" as const,
  }));
  const nodes = keys.map((nodeKey, index) => ({
    nodeKey,
    displayName: text(`${id}.nodes.${index}.name`),
    instruction: text(`${id}.nodes.${index}.instruction`),
    agentDefinitionId: "",
    requiresConfirmation: false,
  }));
  return {
    name: text(`${id}.title`),
    description: text(`${id}.description`),
    supervisorAgentDefinitionId: "",
    maxParallelism: id === "campaign" ? 2 : 1,
    autoRoutable: false,
    nodes: computeAutoLayout(nodes, edges),
    edges,
  };
}
