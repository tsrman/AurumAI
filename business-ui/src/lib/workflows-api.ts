import { api } from "./api";
import type { ApiResponse } from "@/types/api";

export interface WorkflowListItem {
  id: string;
  name: string;
  description: string | null;
  version: number;
  maxParallelism: number;
  status: "draft" | "active" | "archived";
}

export async function listWorkflows(): Promise<WorkflowListItem[]> {
  const response = await api
    .get("workflows")
    .json<ApiResponse<WorkflowListItem[]>>();
  return response.data;
}

export async function cancelWorkflowRun(runId: string): Promise<void> {
  await api.post(`workflow-runs/${encodeURIComponent(runId)}/cancel`);
}

export async function decideWorkflowNode(
  runId: string,
  nodeKey: string,
  decision: "approve" | "reject"
): Promise<void> {
  await api.post(
    `workflow-runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeKey)}/decision`,
    { json: { decision } }
  );
}
