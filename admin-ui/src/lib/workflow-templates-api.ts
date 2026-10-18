import { api } from "@/lib/api";
import type {
  WorkflowDefinition,
  WorkflowEdgeCondition,
} from "@/lib/api";
import type { ApiResponse } from "@/types/api";

export interface TemplateAgent {
  key: string;
  name: string;
  description: string | null;
  modelId: string;
  systemPrompt: string;
  temperature: number;
  skillIds: string[];
  knowledgeBaseIds: string[];
}

export interface TemplateNode {
  nodeKey: string;
  displayName: string;
  agentKey: string;
  instruction: string;
  positionX: number | null;
  positionY: number | null;
  requiresConfirmation: boolean;
}

export interface TemplateEdge {
  sourceNodeKey: string;
  targetNodeKey: string;
  condition: WorkflowEdgeCondition;
  isLoopBack?: boolean;
  maxIterations?: number | null;
}

export interface TemplateSnapshot {
  schemaVersion: 1;
  supervisorAgentKey: string;
  maxParallelism: number;
  agents: TemplateAgent[];
  nodes: TemplateNode[];
  edges: TemplateEdge[];
}

export interface TemplateListItem {
  id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  version: number;
  createdAt: string;
  updatedAt: string;
}

export interface TemplateResponse extends TemplateListItem {
  orgId: string;
  createdBy: string | null;
  updatedBy: string | null;
  snapshot: TemplateSnapshot;
}

export interface TemplateCaptureRequest {
  sourceWorkflowId: string;
  sourceWorkflowVersion: number;
  name: string;
  description?: string | null;
}

export interface TemplateReplaceRequest extends TemplateCaptureRequest {
  version: number;
}

export interface TemplateInstantiateRequest {
  version: number;
  name: string;
}

export interface TemplateInstantiationResponse {
  workflow: WorkflowDefinition;
  agentIds: string[];
}

const singleAttemptMutation = {
  retry: 0,
  context: { noAuthReplay: true },
} as const;

export async function getWorkflowTemplates(includeArchived = false) {
  return api
    .get("admin/workflow-templates", {
      searchParams: { includeArchived: String(includeArchived) },
    })
    .json<ApiResponse<TemplateListItem[]>>();
}

export async function getWorkflowTemplate(id: string) {
  return api
    .get(`admin/workflow-templates/${id}`)
    .json<ApiResponse<TemplateResponse>>();
}

export async function captureWorkflowTemplate(payload: TemplateCaptureRequest) {
  return api
    .post("admin/workflow-templates", {
      json: payload,
      ...singleAttemptMutation,
    })
    .json<ApiResponse<TemplateResponse>>();
}

export async function replaceWorkflowTemplate(
  id: string,
  payload: TemplateReplaceRequest
) {
  return api
    .put(`admin/workflow-templates/${id}`, {
      json: payload,
      ...singleAttemptMutation,
    })
    .json<ApiResponse<TemplateResponse>>();
}

export async function instantiateWorkflowTemplate(
  id: string,
  payload: TemplateInstantiateRequest
) {
  return api
    .post(`admin/workflow-templates/${id}/instantiate`, {
      json: payload,
      ...singleAttemptMutation,
    })
    .json<ApiResponse<TemplateInstantiationResponse>>();
}

export async function archiveWorkflowTemplate(id: string, version: number) {
  return api
    .post(`admin/workflow-templates/${id}/archive`, {
      json: { version },
      ...singleAttemptMutation,
    })
    .json<ApiResponse<TemplateResponse>>();
}
