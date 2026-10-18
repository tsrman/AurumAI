import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import AdminWorkflowsPage from "@/app/admin/workflows/page";
import type { WorkflowDefinition, WorkflowListItem } from "@/lib/api";
import type { TemplateResponse } from "@/lib/workflow-templates-api";

const state = vi.hoisted(() => ({
  access: {
    allowed: false,
    canWrite: false,
    isLoading: false,
    error: "The organization administrator role is required." as string | null,
  },
  workflows: [] as WorkflowListItem[],
  template: null as TemplateResponse | null,
  useWorkflows: vi.fn(),
  useWorkflow: vi.fn(),
  useWorkflowTemplates: vi.fn(),
  useAgentsQuery: vi.fn(),
  agentsRefetch: vi.fn(),
  instantiate: vi.fn(),
}));

const copiedWorkflow: WorkflowDefinition = {
  id: "workflow-copy",
  orgId: "org-1",
  name: "Independent copy",
  description: null,
  version: 1,
  status: "active",
  supervisorAgentDefinitionId: "agent-1",
  maxParallelism: 1,
  autoRoutable: false,
  nodes: [{
    nodeKey: "review",
    displayName: "Review",
    agentDefinitionId: "agent-1",
    instruction: "Review",
    requiresConfirmation: true,
  }],
  edges: [],
  createdAt: "2026-09-05T00:00:00Z",
  updatedAt: "2026-09-05T00:00:00Z",
};

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state.useAgentsQuery(),
}));
vi.mock("@/hooks/useAdminAccess", () => ({
  useAdminAccess: () => state.access,
}));
vi.mock("@/hooks/useWorkflows", () => ({
  useWorkflows: () => state.useWorkflows(),
  useWorkflow: (id: string | null) => state.useWorkflow(id),
  useWorkflowDiagnostic: () => ({ data: undefined }),
  useCreateWorkflow: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useReplaceWorkflow: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useArchiveWorkflow: () => ({ mutate: vi.fn() }),
  useValidateWorkflow: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));
vi.mock("@/hooks/useWorkflowTemplates", () => ({
  useWorkflowTemplates: () => state.useWorkflowTemplates(),
  useWorkflowTemplate: () => ({
    data: state.template ? { data: state.template } : undefined,
    refetch: vi.fn(),
  }),
  useCaptureWorkflowTemplate: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useReplaceWorkflowTemplate: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useInstantiateWorkflowTemplate: () => ({ mutateAsync: state.instantiate, isPending: false }),
  useArchiveWorkflowTemplate: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  state.access = {
    allowed: false,
    canWrite: false,
    isLoading: false,
    error: "The organization administrator role is required.",
  };
  state.workflows = [];
  state.template = null;
  state.useWorkflows.mockImplementation(() => ({
    data: { data: state.workflows },
    isLoading: false,
  }));
  state.useWorkflow.mockImplementation((id: string | null) => ({
    data: id === "workflow-copy" ? { data: copiedWorkflow } : undefined,
    refetch: vi.fn(),
  }));
  state.useWorkflowTemplates.mockImplementation(() => ({
    data: { data: state.template ? [state.template] : [] },
    isLoading: false,
    refetch: vi.fn(),
  }));
  state.useAgentsQuery.mockImplementation(() => ({
    data: { data: [{ id: "agent-1", name: "Agent" }] },
    isLoading: false,
    refetch: state.agentsRefetch,
  }));
  state.instantiate.mockResolvedValue({
    data: { workflow: copiedWorkflow, agentIds: ["agent-1"] },
  });
});

test("denied users see no workflow controls and start no protected requests", () => {
  render(<AdminWorkflowsPage />);

  expect(
    screen.getByText("The organization administrator role is required.")
  ).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Workflow" })).not.toBeInTheDocument();
  expect(screen.queryByText("Workflow templates")).not.toBeInTheDocument();
  expect(state.useWorkflows).not.toHaveBeenCalled();
  expect(state.useWorkflowTemplates).not.toHaveBeenCalled();
  expect(state.useAgentsQuery).not.toHaveBeenCalled();
});

test("the table selects only an active saved workflow as the template source", async () => {
  state.access = { allowed: true, canWrite: true, isLoading: false, error: null };
  state.workflows = [
    { id: "active", name: "Active", version: 2, maxParallelism: 1, status: "active" },
    { id: "archived", name: "Archived", version: 1, maxParallelism: 1, status: "archived" },
  ];
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);

  const activeRow = screen.getByRole("row", { name: /Active/ });
  const archivedRow = screen.getByRole("row", { name: /Archived/ });
  expect(screen.getByLabelText("Source workflow")).toHaveValue("");
  await user.click(
    within(activeRow).getByRole("button", { name: "Save as template" })
  );
  expect(screen.getByLabelText("Source workflow")).toHaveValue("active");
  expect(
    within(archivedRow).queryByRole("button", { name: "Save as template" })
  ).not.toBeInTheDocument();
});

test("a created copy opens in the editor, reloads agents, and preserves HITL", async () => {
  state.access = { allowed: true, canWrite: true, isLoading: false, error: null };
  state.template = {
    id: "template-1",
    orgId: "org-1",
    name: "Template",
    description: null,
    status: "active",
    version: 3,
    createdBy: "user-1",
    updatedBy: "user-1",
    createdAt: "2026-09-05T00:00:00Z",
    updatedAt: "2026-09-05T00:00:00Z",
    snapshot: {
      schemaVersion: 1,
      supervisorAgentKey: "agent",
      maxParallelism: 1,
      agents: [],
      nodes: [],
      edges: [],
    },
  };
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);

  await user.selectOptions(screen.getByLabelText("Workflow template"), "template-1");
  await user.type(screen.getByLabelText("Workflow copy name"), "Independent copy");
  await user.click(screen.getByRole("button", { name: "Create workflow from template" }));

  await waitFor(() => expect(state.useWorkflow).toHaveBeenCalledWith("workflow-copy"));
  expect(state.agentsRefetch).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("heading", { name: "Edit workflow" })).toBeInTheDocument();
  expect(screen.getByLabelText("Requires confirmation — node 1")).toBeChecked();
});
