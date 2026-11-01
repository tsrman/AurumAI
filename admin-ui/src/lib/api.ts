import ky, { HTTPError } from "ky";
import { ApiResponse } from "@/types/api";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { LocalizedError } from "./errors";

declare global {
  interface Window {
    keycloak?: {
      token?: string;
      updateToken: (min: number) => Promise<boolean>;
    };
  }
}

export interface Group {
  id: string;
  orgId?: string | null;
  name: string;
  keycloakId?: string | null;
  lastSyncedAt?: string | null;
  memberCount?: number;
}

export interface Me {
  user_id: string;
  email?: string | null;
  full_name?: string | null;
  org_id?: string | null;
  org_name?: string | null;
  role: string;
}

export interface GroupCreate {
  name: string;
}

export interface GroupUpdate {
  name?: string;
}

export interface Skill {
  id: string;
  orgId?: string | null;
  name: string;
  description?: string | null;
  type: "agent" | "tool" | "mcp" | "code";
  workflowType?: string | null;
  activityName?: string | null;
  mcpTransport?: string | null;
  mcpEndpoint?: string | null;
  mcpAuthToken?: string | null;
  // mcpAuthToken always comes back null to browsers —
  // this is the only way to tell "a token is saved" apart from "no token
  // was ever set".
  hasMcpAuthToken?: boolean;
  inputSchema: Record<string, unknown>;
  allowedChildSkills?: string[] | null;
  bundleSkillName?: string | null;
  bundleSizeBytes?: number | null;
  bundleChecksum?: string | null;
  networkPolicy?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface SkillCreate {
  name: string;
  description?: string;
  type: "agent" | "tool" | "mcp";
  workflow_type?: string;
  activity_name?: string;
  mcp_transport?: string;
  mcp_endpoint?: string;
  mcp_auth_token?: string;
  input_schema?: Record<string, unknown>;
  allowed_child_skills?: string[];
}

export interface SkillUpdate {
  name?: string;
  description?: string;
  type?: "agent" | "tool" | "mcp";
  workflow_type?: string;
  activity_name?: string;
  mcp_transport?: string;
  mcp_endpoint?: string;
  mcp_auth_token?: string;
  input_schema?: Record<string, unknown>;
  allowed_child_skills?: string[];
}

export interface McpToolInfo {
  name: string;
  description?: string | null;
  inputSchema: Record<string, unknown>;
}

export interface Model {
  id: string;
  object?: string;
}

export type GatewayStatus = "available" | "mock" | "unavailable";

export interface AdminModelEntry {
  id: string;
  organizationReadable: boolean;
  isDefault: boolean;
}

export interface AdminModelSettings {
  gatewayStatus: GatewayStatus;
  defaultModel: string | null;
  defaultAvailable: boolean;
  defaultTemperature: number | null;
  models: AdminModelEntry[];
}

export interface OrganizationModelSetting {
  defaultModel: string | null;
  defaultTemperature: number | null;
}

export interface UsageTotals {
  requests: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  unaccountedRequests: number;
  // Images API calls; each is also counted in requests.
  images: number;
}

export interface DailyUsage extends UsageTotals {
  date: string;
}

export interface UserUsage extends UsageTotals {
  userId: string;
  email: string;
  fullName: string | null;
}

export interface ModelUsage extends UsageTotals {
  model: string;
}

/** Monthly quota consumption — never derived from the report's period. */
export interface UsageQuota {
  periodStart: string;
  requestsUsed: number;
  tokensUsed: number;
  requestsRemaining: number | null;
  tokensRemaining: number | null;
}

export interface AdminUsageReport {
  period: { from: string; to: string; timezone: "UTC" };
  limits: { requestLimit: number | null; tokenLimit: number | null };
  quota: UsageQuota;
  totals: UsageTotals;
  daily: DailyUsage[];
  /** Top spenders only — the backend caps this list. */
  byUser: UserUsage[];
  /** How many users had usage at all, so a capped byUser can say so. */
  byUserTotal: number;
  byModel: ModelUsage[];
}

export interface AdminUsageFilters {
  from?: string;
  to?: string;
  userId?: string;
  model?: string;
}

export interface AdminUser {
  id: string;
  email: string;
  fullName: string | null;
  role: string;
  status: string;
  lastLoginAt: string | null;
  createdAt: string;
}

export interface AdminUserFilters {
  limit: number;
  offset: number;
  search?: string;
  status?: string;
  groupId?: string;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description?: string | null;
  canWrite: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface AgentDefinition {
  id: string;
  name: string;
  description?: string | null;
  modelId: string;
  systemPrompt: string;
  temperature: number | null;
  skillIds: string[];
  knowledgeBaseIds: string[];
  // "hermes": a workflow node run by Hermes Agent on the organization's Hermes model.
  kind?: AgentKind;
  // Built-in capabilities a chat turn on the agent may use.
  webEnabled?: boolean;
  imagesEnabled?: boolean;
  askUserEnabled?: boolean;
  // Set when archived: hidden from pickers and refused for new use; history stays.
  archivedAt?: string | null;
  canWrite: boolean;
  createdAt: string;
  updatedAt: string;
}

export type AgentKind = "agent" | "hermes";

export interface AgentDefinitionCreate {
  // Fixed at creation.
  kind?: AgentKind;
  name: string;
  description?: string;
  modelId: string;
  systemPrompt: string;
  temperature: number | null;
  skillIds: string[];
  knowledgeBaseIds: string[];
  webEnabled?: boolean;
  imagesEnabled?: boolean;
  askUserEnabled?: boolean;
}

export type ResourceType =
  | "model"
  | "skill"
  | "knowledge_base"
  | "agent_definition"
  | "workflow";
export type PrincipalType = "organization" | "group" | "user";
export type AccessPermission = "read" | "write";

export interface CapabilityDefinition {
  key: string;
  label: string;
  description: string;
}

export interface CapabilityAssignment {
  capabilities: string[];
}

export interface AuthorizationPrincipal {
  principalType: PrincipalType;
  principalId: string;
  label: string;
}

export interface GrantSpec {
  principalType: PrincipalType;
  principalId: string;
  permission: AccessPermission;
}

export interface AccessGrant extends GrantSpec {
  id: string;
  resourceType: ResourceType;
  resourceId: string;
  createdBy?: string | null;
  createdVia: string;
}

export interface GrantReconcileRequest {
  resourceType: ResourceType;
  resourceId: string;
  grants: GrantSpec[];
}

export interface PreviewResourceRequest {
  resourceType: ResourceType;
  resourceId: string;
}

export interface PreviewResource extends PreviewResourceRequest {
  canRead: boolean;
  canWrite: boolean;
  source: string;
}

export interface AccessPreviewResponse {
  subjectType: "group" | "user";
  subjectId: string;
  adminBypass: boolean;
  capabilities: string[];
  resources: PreviewResource[];
}

export interface AccessPreviewRequest {
  subjectType: "group" | "user";
  subjectId: string;
  resources: PreviewResourceRequest[];
}

export interface WorkflowNodeDraft {
  nodeKey: string;
  displayName: string;
  agentDefinitionId: string;
  instruction: string;
  requiresConfirmation?: boolean;
  positionX?: number | null;
  positionY?: number | null;
}

export type WorkflowEdgeCondition = "on_success" | "on_failure";

export interface WorkflowEdgeDraft {
  sourceNodeKey: string;
  targetNodeKey: string;
  condition?: WorkflowEdgeCondition;
  isLoopBack?: boolean;
  maxIterations?: number | null;
}

export interface WorkflowDraft {
  name: string;
  description?: string | null;
  supervisorAgentDefinitionId: string;
  maxParallelism: number;
  autoRoutable?: boolean;
  nodes: WorkflowNodeDraft[];
  edges: WorkflowEdgeDraft[];
}

export interface WorkflowDefinition extends WorkflowDraft {
  id: string;
  orgId: string;
  version: number;
  status: "active" | "archived";
  createdBy?: string | null;
  updatedBy?: string | null;
  createdAt: string;
  updatedAt: string;
  sourceTemplateId?: string | null;
  sourceTemplateVersion?: number | null;
}

export interface WorkflowListItem {
  id: string;
  name: string;
  description?: string | null;
  version: number;
  maxParallelism: number;
  status: "active" | "archived";
  autoRoutable?: boolean;
}

export interface WorkflowValidation {
  valid: boolean;
  topologicalOrder: string[];
  predecessors: Record<string, string[]>;
}

export interface WorkflowNodeDiagnostic {
  phaseKey: string;
  agentDefinitionId: string;
  agentName: string;
  status: string;
  attemptCount: number;
  errorCode: string | null;
  startedAt: string | null;
  completedAt: string | null;
  output: string | null;
  internalError: string | null;
  diagnosticExpiresAt: string | null;
  outputExpired: boolean;
}

export interface WorkflowRunDiagnostic {
  id: string;
  workflowDefinitionId: string;
  workflowDefinitionVersion: number;
  conversationId: string;
  turnId: string;
  status: string;
  errorCode: string | null;
  internalError: string | null;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
  nodes: WorkflowNodeDiagnostic[];
}

export const api = ky.create({
  prefixUrl: getRuntimeConfig().apiUrl,
  timeout: 30000,
  retry: {
    limit: 3,
    // POST/PATCH are not idempotent here — retrying a
    // POST that committed before the response came back (network drop,
    // upstream 5xx after success) creates a duplicate (e.g. POST
    // /v1/skills, /v1/skills/code — no unique constraint on name), and
    // POST /v1/skills/discover-mcp-tools returns 502 as an ordinary "could
    // not connect" result, not a transient gateway failure — retrying it
    // just triples the connection attempts and delays showing the real
    // error. GET/PUT are ky's own default retry-safe set.
    methods: ["get", "put"],
    statusCodes: [429, 500, 502, 503],
    backoffLimit: 8000,
  },
  hooks: {
    beforeRequest: [
      (request) => {
        const token = window.keycloak?.token;
        if (token) request.headers.set("Authorization", `Bearer ${token}`);
      },
    ],
    afterResponse: [
      async (request, options, response) => {
        if (response.status === 401) {
          if (options.context.noAuthReplay === true) return response;
          // an expired/invalid refresh token makes
          // keycloak-js's own updateToken() reject — unguarded, that
          // exception replaced the original 401 HTTPError for every caller
          // that checks response.status, with keycloak's own error
          // instead. Fall through to the original 401 response on any
          // refresh failure, same as when refreshed=false.
          try {
            const refreshed = await window.keycloak?.updateToken(30);
            if (refreshed) {
              request.headers.set("Authorization", `Bearer ${window.keycloak?.token}`);
              return ky(request);
            }
          } catch {
            // fall through to `return response` below
          }
        }
        return response;
      },
    ],
  },
});

export async function getMe() {
  return (await api.get("auth/me")).json<ApiResponse<Me>>();
}

export async function getGroups() {
  return (await api.get("admin/groups")).json<ApiResponse<Group[]>>();
}

export async function createGroup(payload: GroupCreate) {
  return (await api.post("admin/groups", { json: payload })).json<
    ApiResponse<Group>
  >();
}

export async function updateGroup(id: string, payload: GroupUpdate) {
  return (await api.patch(`admin/groups/${id}`, { json: payload })).json<
    ApiResponse<Group>
  >();
}

export async function deleteGroup(id: string) {
  return api.delete(`admin/groups/${id}`);
}

export async function getSkills() {
  return (await api.get("skills")).json<ApiResponse<Skill[]>>();
}

export async function createSkill(payload: SkillCreate) {
  return (await api.post("skills", { json: payload })).json<ApiResponse<Skill>>();
}

export async function updateSkill(id: string, payload: SkillUpdate) {
  return (await api.patch(`skills/${id}`, { json: payload })).json<
    ApiResponse<Skill>
  >();
}

// business-backend/src/services/workflow_graph.py::WorkflowGraphError codes. The
// generic HTTPException handler (middleware/error_handler.py) wraps every error
// body as {error: {code: "FORBIDDEN"|"UNAUTHORIZED", message: <exc.detail>}} —
// message IS the {code, nodeKeys} object for a WorkflowGraphError, never a
// top-level `detail` field (see unwrapWorkflowError below).
// Codes with a dedicated message under `errors.workflowGraph`.
const WORKFLOW_GRAPH_ERROR_CODES = new Set([
  "empty_graph",
  "self_edge",
  "cycle",
  "dangling_edge",
  "duplicate_node_key",
  "duplicate_edge",
  "too_many_nodes",
  "reserved_node_key",
  "invalid_loop_edge",
  "overlapping_loop_bodies",
  "loop_body_requires_confirmation",
  "loop_body_fan_out",
]);

export function describeWorkflowGraphError(detail: { code: string; nodeKeys?: string[] }): LocalizedError {
  const nodeKeys = detail.nodeKeys ?? [];
  const known = WORKFLOW_GRAPH_ERROR_CODES.has(detail.code);
  return new LocalizedError(known ? `workflowGraph.${detail.code}` : "workflowGraph.unknown", {
    values: known ? undefined : { code: detail.code },
    detail: nodeKeys.length > 0 ? nodeKeys.join(", ") : undefined,
  });
}

function isWorkflowGraphErrorDetail(value: unknown): value is { code: string; nodeKeys?: string[] } {
  return !!value && typeof value === "object" && typeof (value as { code?: unknown }).code === "string";
}

async function unwrapWorkflowError<T>(promise: Promise<T>): Promise<T> {
  try {
    return await promise;
  } catch (err) {
    if (err instanceof HTTPError) {
      const message = await err.response
        .clone()
        .json()
        .then((body: { error?: { message?: unknown } }) => body.error?.message)
        .catch(() => undefined);
      if (typeof message === "string") throw new Error(message);
      if (isWorkflowGraphErrorDetail(message)) throw describeWorkflowGraphError(message);
    }
    throw err;
  }
}

async function unwrapUploadError<T>(promise: Promise<T>): Promise<T> {
  try {
    return await promise;
  } catch (err) {
    if (err instanceof HTTPError) {
      const detail = await err.response
        .clone()
        .json()
        .then((body: { detail?: string; error?: { message?: string } }) => body.error?.message || body.detail)
        .catch(() => undefined);
      if (detail) throw new Error(detail);
    }
    throw err;
  }
}

export async function uploadCodeSkill(form: FormData) {
  return unwrapUploadError(
    (async () => (await api.post("skills/code", { body: form })).json<ApiResponse<Skill>>())()
  );
}

export async function replaceCodeSkillBundle(id: string, form: FormData) {
  return unwrapUploadError(
    (async () =>
      (await api.put(`skills/code/${id}/bundle`, { body: form })).json<ApiResponse<Skill>>())()
  );
}

export async function discoverMcpTools(
  mcpEndpoint: string,
  mcpTransport: string,
  mcpAuthToken?: string,
  // when editing an existing MCP skill and the admin
  // hasn't retyped its token, this lets the backend fall back to the
  // stored token server-side — mcpAuthToken itself never comes back to the
  // browser to resend.
  skillId?: string
) {
  try {
    return await (
      await api.post("skills/discover-mcp-tools", {
        json: {
          mcpEndpoint,
          mcpTransport,
          mcpAuthToken: mcpAuthToken || undefined,
          skillId: skillId || undefined,
        },
      })
    ).json<ApiResponse<McpToolInfo[]>>();
  } catch (err) {
    if (err instanceof HTTPError) {
      const detail = await err.response
        .clone()
        .json()
        .then((body: { error?: { message?: string } }) => body.error?.message)
        .catch(() => undefined);
      if (detail) throw new Error(detail);
    }
    throw err;
  }
}

export async function deleteSkill(id: string) {
  return api.delete(`skills/${id}`);
}

export async function getModels() {
  return (await api.get("models")).json<ApiResponse<Model[]>>();
}

export async function getAdminModels() {
  return (await api.get("admin/models")).json<ApiResponse<AdminModelSettings>>();
}

export async function putOrganizationModelSettings(settings: {
  defaultModel: string | null;
  defaultTemperature: number | null;
}) {
  return (
    await api.put("admin/settings/models", { json: settings })
  ).json<ApiResponse<OrganizationModelSetting>>();
}

export interface OrganizationImageModels {
  enabled: boolean;
  gatewayStatus: GatewayStatus;
  generationModel: string | null;
  editModel: string | null;
  models: string[];
  // Chat models marked as accepting images, picked from chatModels.
  visionModels: string[];
  chatModels: string[];
}

export async function getOrganizationImageModels() {
  return (await api.get("admin/settings/images")).json<
    ApiResponse<OrganizationImageModels>
  >();
}

export async function putOrganizationImageModels(settings: {
  generationModel: string | null;
  editModel: string | null;
  visionModels?: string[];
}) {
  return (await api.put("admin/settings/images", { json: settings })).json<
    ApiResponse<OrganizationImageModels>
  >();
}

export type HermesEgressMode = "none" | "packages" | "public";

export interface OrganizationHermesSettings {
  // The operator switch (APP_HERMES_ENABLED).
  enabled: boolean;
  egressMode: HermesEgressMode;
  egressMaxMode: HermesEgressMode;
  model: string | null;
}

export async function getOrganizationHermesSettings() {
  return (await api.get("admin/settings/hermes")).json<
    ApiResponse<OrganizationHermesSettings>
  >();
}

export async function putOrganizationHermesSettings(settings: {
  egressMode?: HermesEgressMode;
  model?: string | null;
}) {
  return (await api.put("admin/settings/hermes", { json: settings })).json<
    ApiResponse<OrganizationHermesSettings>
  >();
}

export type HermesSkillDraftStatus = "pending" | "approved" | "rejected";

/** A skill Hermes Agent wrote for itself in a user's sandbox. */
export interface HermesSkillDraft {
  id: string;
  path: string;
  name: string;
  description: string;
  status: HermesSkillDraftStatus;
  fileCount: number;
  sizeBytes: number;
  skillId: string | null;
  authorEmail: string | null;
  // A decision names the version it was made on: a later turn can replace the files.
  bundleChecksum: string;
  createdAt: string;
  updatedAt: string;
}

export interface HermesSkillDraftDetail extends HermesSkillDraft {
  files: { path: string; content: string }[];
}

export async function listHermesSkillDrafts(status: HermesSkillDraftStatus) {
  return (await api.get("admin/hermes/skill-drafts", { searchParams: { status } })).json<
    ApiResponse<HermesSkillDraft[]>
  >();
}

export async function getHermesSkillDraft(id: string) {
  return (await api.get(`admin/hermes/skill-drafts/${id}`)).json<ApiResponse<HermesSkillDraftDetail>>();
}

export async function approveHermesSkillDraft(id: string, decision: { bundleChecksum: string; name?: string }) {
  return (await api.post(`admin/hermes/skill-drafts/${id}/approve`, { json: decision })).json<
    ApiResponse<HermesSkillDraft>
  >();
}

export async function rejectHermesSkillDraft(id: string, decision: { bundleChecksum: string }) {
  return (await api.post(`admin/hermes/skill-drafts/${id}/reject`, { json: decision })).json<
    ApiResponse<HermesSkillDraft>
  >();
}

function explicitSearchParams<T extends object>(
  values: T
): Record<string, string | number> {
  return Object.fromEntries(
    Object.entries(values).filter(
      (entry): entry is [string, string | number] =>
        entry[1] !== undefined && entry[1] !== ""
    )
  );
}

export async function getAdminUsage(filters: AdminUsageFilters) {
  return (
    await api.get("admin/analytics/usage", {
      searchParams: explicitSearchParams(filters),
    })
  ).json<ApiResponse<AdminUsageReport>>();
}

export async function getAdminUsers(filters: AdminUserFilters) {
  return (
    await api.get("admin/users", {
      searchParams: explicitSearchParams(filters),
    })
  ).json<ApiResponse<AdminUser[]>>();
}

export async function getKnowledgeBases() {
  return (await api.get("knowledge-bases")).json<ApiResponse<KnowledgeBase[]>>();
}

const AGENT_ERROR_CODES = new Set(["agent_in_use", "agent_has_history", "agent_in_active_run", "agent_name_archived"]);

/** Why an agent cannot be deleted, archived or named so, in the active locale; workflows named by the backend are listed. */
export function describeAgentError(detail: { code: string; workflows?: { name: string }[] }): LocalizedError {
  const known = AGENT_ERROR_CODES.has(detail.code);
  const names = (detail.workflows ?? []).map((workflow) => workflow.name);
  return new LocalizedError(known ? `agent.${detail.code}` : "agent.unknown", {
    values: known ? undefined : { code: detail.code },
    detail: names.length > 0 ? names.join(", ") : undefined,
  });
}

async function unwrapAgentError<T>(promise: Promise<T>): Promise<T> {
  try {
    return await promise;
  } catch (err) {
    if (err instanceof HTTPError) {
      const message = await err.response
        .clone()
        .json()
        .then((body: { error?: { message?: unknown } }) => body.error?.message)
        .catch(() => undefined);
      if (typeof message === "string") throw new Error(message);
      if (isWorkflowGraphErrorDetail(message)) throw describeAgentError(message);
    }
    throw err;
  }
}

export async function getAgentDefinitions(includeArchived = false) {
  return (
    await api.get("agent-definitions", {
      searchParams: { includeArchived: String(includeArchived) },
    })
  ).json<ApiResponse<AgentDefinition[]>>();
}

export async function createAgentDefinition(payload: AgentDefinitionCreate) {
  return unwrapAgentError(
    api.post("agent-definitions", { json: payload }).json<ApiResponse<AgentDefinition>>(),
  );
}

export type AgentDefinitionUpdate = Partial<AgentDefinitionCreate>;

export async function getAgentDefinition(id: string) {
  return (await api.get(`agent-definitions/${id}`)).json<
    ApiResponse<AgentDefinition>
  >();
}

export async function updateAgentDefinition(id: string, payload: AgentDefinitionUpdate) {
  return unwrapAgentError(
    api.patch(`agent-definitions/${id}`, { json: payload }).json<ApiResponse<AgentDefinition>>(),
  );
}

export async function deleteAgentDefinition(id: string) {
  return unwrapAgentError(api.delete(`agent-definitions/${id}`));
}

export async function archiveAgentDefinition(id: string) {
  return unwrapAgentError(api.post(`agent-definitions/${id}/archive`).json<ApiResponse<AgentDefinition>>());
}

export async function unarchiveAgentDefinition(id: string) {
  return unwrapAgentError(api.post(`agent-definitions/${id}/unarchive`).json<ApiResponse<AgentDefinition>>());
}

export async function getAdminWorkflows(includeArchived = false) {
  return (
    await api.get("admin/workflows", {
      searchParams: { includeArchived: String(includeArchived) },
    })
  ).json<ApiResponse<WorkflowListItem[]>>();
}

export async function getAdminWorkflow(id: string) {
  return (await api.get(`admin/workflows/${id}`)).json<
    ApiResponse<WorkflowDefinition>
  >();
}

export async function createWorkflow(payload: WorkflowDraft) {
  return unwrapWorkflowError(
    (async () =>
      (await api.post("admin/workflows", { json: payload })).json<
        ApiResponse<WorkflowDefinition>
      >())()
  );
}

export async function replaceWorkflow(
  id: string,
  version: number,
  payload: WorkflowDraft
) {
  return unwrapWorkflowError(
    (async () =>
      (
        await api.put(`admin/workflows/${id}`, {
          json: { ...payload, version },
        })
      ).json<ApiResponse<WorkflowDefinition>>())()
  );
}

export async function archiveWorkflow(id: string) {
  return api.delete(`admin/workflows/${id}`);
}

export async function validateWorkflow(payload: WorkflowDraft) {
  return unwrapWorkflowError(
    (async () =>
      (await api.post("admin/workflows/validate", { json: payload })).json<
        ApiResponse<WorkflowValidation>
      >())()
  );
}

export async function getWorkflowDiagnostic(runId: string) {
  return (await api.get(`admin/workflow-runs/${runId}`)).json<
    ApiResponse<WorkflowRunDiagnostic>
  >();
}

export async function getAuthorizationSubjects() {
  return (await api.get("admin/authorization/subjects")).json<
    ApiResponse<AuthorizationPrincipal[]>
  >();
}

export async function getCapabilityCatalog() {
  return (await api.get("admin/authorization/capabilities")).json<
    ApiResponse<CapabilityDefinition[]>
  >();
}

export async function getOrganizationCapabilities() {
  return (await api.get("admin/authorization/capabilities/organization")).json<
    ApiResponse<CapabilityAssignment>
  >();
}

export async function putOrganizationCapabilities(capabilities: string[]) {
  return (
    await api.put("admin/authorization/capabilities/organization", {
      json: { capabilities },
    })
  ).json<ApiResponse<CapabilityAssignment>>();
}

export async function getGroupCapabilities(groupId: string) {
  return (await api.get(`admin/authorization/capabilities/groups/${groupId}`)).json<
    ApiResponse<CapabilityAssignment>
  >();
}

export async function putGroupCapabilities(groupId: string, capabilities: string[]) {
  return (
    await api.put(`admin/authorization/capabilities/groups/${groupId}`, {
      json: { capabilities },
    })
  ).json<ApiResponse<CapabilityAssignment>>();
}

export async function getResourceGrants(
  resourceType: ResourceType,
  resourceId: string
) {
  return (
    await api.get("admin/authorization/grants", {
      searchParams: { resourceType, resourceId },
    })
  ).json<ApiResponse<AccessGrant[]>>();
}

export async function reconcileResourceGrants(payload: GrantReconcileRequest) {
  return (
    await api.put("admin/authorization/grants/reconcile", { json: payload })
  ).json<ApiResponse<AccessGrant[]>>();
}

export async function previewAuthorization(payload: AccessPreviewRequest) {
  return (
    await api.post(
      `admin/authorization/preview/${payload.subjectType}s/${payload.subjectId}`,
      { json: { resources: payload.resources } }
    )
  ).json<ApiResponse<AccessPreviewResponse>>();
}
