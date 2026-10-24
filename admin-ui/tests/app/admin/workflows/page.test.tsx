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
  replace: vi.fn(),
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
  nodes: [
    {
      nodeKey: "review",
      displayName: "Review",
      agentDefinitionId: "agent-1",
      instruction: "Review",
      requiresConfirmation: true,
    },
  ],
  edges: [],
  createdAt: "2026-09-05T00:00:00Z",
  updatedAt: "2026-09-05T00:00:00Z",
};

vi.mock("@/components/authorization/GrantEditor", () => ({
  GrantEditor: () => null,
}));
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
  useReplaceWorkflow: () => ({ isPending: false, mutateAsync: state.replace }),
  useArchiveWorkflow: () => ({ mutate: vi.fn() }),
  useValidateWorkflow: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));
vi.mock("@/hooks/useWorkflowTemplates", () => ({
  useWorkflowTemplates: () => state.useWorkflowTemplates(),
  useWorkflowTemplate: () => ({
    data: state.template ? { data: state.template } : undefined,
    refetch: vi.fn(),
  }),
  useCaptureWorkflowTemplate: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useReplaceWorkflowTemplate: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useInstantiateWorkflowTemplate: () => ({
    mutateAsync: state.instantiate,
    isPending: false,
  }),
  useArchiveWorkflowTemplate: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
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
  state.replace.mockResolvedValue({ data: { ...copiedWorkflow, version: 2 } });
  state.instantiate.mockResolvedValue({
    data: { workflow: copiedWorkflow, agentIds: ["agent-1"] },
  });
});

test("denied users see no workflow controls and start no protected requests", () => {
  render(<AdminWorkflowsPage />);

  expect(
    screen.getByText("The organization administrator role is required."),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Workflow" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText("Workflow templates")).not.toBeInTheDocument();
  expect(state.useWorkflows).not.toHaveBeenCalled();
  expect(state.useWorkflowTemplates).not.toHaveBeenCalled();
  expect(state.useAgentsQuery).not.toHaveBeenCalled();
});

test("the table selects only an active saved workflow as the template source", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  state.workflows = [
    {
      id: "active",
      name: "Active",
      version: 2,
      maxParallelism: 1,
      status: "active",
    },
    {
      id: "archived",
      name: "Archived",
      version: 1,
      maxParallelism: 1,
      status: "archived",
    },
  ];
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);

  await user.click(screen.getByRole("tab", { name: "Saved templates" }));
  expect(screen.getByLabelText("Source workflow")).toHaveValue("");
  await user.click(screen.getByRole("tab", { name: "My workflows" }));
  await user.click(
    within(screen.getByRole("row", { name: /Active/ })).getByRole("button", {
      name: "Save as template",
    }),
  );
  expect(screen.getByLabelText("Source workflow")).toHaveValue("active");
  await user.click(screen.getByRole("tab", { name: "My workflows" }));
  expect(
    within(screen.getByRole("row", { name: /Archived/ })).queryByRole(
      "button",
      { name: "Save as template" },
    ),
  ).not.toBeInTheDocument();
});

test("a created copy opens in the editor, reloads agents, and preserves HITL", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
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

  await user.click(screen.getByRole("tab", { name: "Saved templates" }));
  await user.selectOptions(
    screen.getByLabelText("Workflow template"),
    "template-1",
  );
  await user.type(
    screen.getByLabelText("Workflow copy name"),
    "Independent copy",
  );
  await user.click(
    screen.getByRole("button", { name: "Create workflow from template" }),
  );

  await waitFor(() =>
    expect(state.useWorkflow).toHaveBeenCalledWith("workflow-copy"),
  );
  expect(state.agentsRefetch).toHaveBeenCalledTimes(1);
  expect(
    screen.getByRole("heading", { name: "Edit workflow" }),
  ).toBeInTheDocument();
  expect(screen.getByText(/The copy has independent agents/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "3. Steps" }));
  expect(screen.getByLabelText("Requires confirmation — node 1")).toBeChecked();
});

test("starts with a task overview and previews examples without creating resources", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);
  expect(screen.getByRole("button", { name: "Create workflow" })).toBeVisible();
  expect(screen.queryByLabelText("Workflow name")).not.toBeInTheDocument();
  await user.click(screen.getByRole("tab", { name: "Examples" }));
  await user.click(
    screen.getByRole("button", { name: "Preview Customer support reply" }),
  );
  expect(
    screen.getByText(/Draft an English reply to Maya about order A-1042/),
  ).toBeVisible();
  expect(screen.getByText(/The fee was EUR 8/)).toBeVisible();
  expect(screen.queryByLabelText("Workflow name")).not.toBeInTheDocument();
  expect(state.instantiate).not.toHaveBeenCalled();
});

test("keeps a dirty draft until navigation is explicitly confirmed", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);
  await user.click(screen.getByRole("button", { name: "Create workflow" }));
  await user.type(screen.getByLabelText("Workflow name"), "Keep this draft");
  await user.click(screen.getByRole("tab", { name: "Examples" }));
  await user.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(screen.getByLabelText("Workflow name")).toHaveValue("Keep this draft");
  await user.click(screen.getByRole("tab", { name: "Examples" }));
  await user.click(screen.getByRole("button", { name: "Discard changes" }));
  expect(screen.queryByLabelText("Workflow name")).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Preview Customer support reply" }),
  ).toBeVisible();
});

test("loads an example locally and requires explicit agent assignment before saving", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);
  await user.click(screen.getByRole("tab", { name: "Examples" }));
  await user.click(
    screen.getByRole("button", { name: "Preview Workshop campaign plan" }),
  );
  await user.click(screen.getByRole("button", { name: "Use this example" }));
  expect(screen.getByLabelText("Workflow name")).toHaveValue(
    "Workshop campaign plan",
  );
  await user.click(screen.getByRole("button", { name: "4. Check" }));
  expect(screen.getByText("Choose a supervisor.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "2. Agents" }));
  await user.selectOptions(screen.getByLabelText("Supervisor"), "agent-1");
  await user.click(
    screen.getByRole("button", { name: "Use supervisor for all steps" }),
  );
  await user.click(screen.getByRole("button", { name: "4. Check" }));
  expect(screen.getByRole("button", { name: "Validate" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(state.instantiate).not.toHaveBeenCalled();
});

test("read-only administrators can explore examples without mutation controls", async () => {
  state.access = {
    allowed: true,
    canWrite: false,
    isLoading: false,
    error: null,
  };
  state.workflows = [
    {
      id: "a",
      name: "Read-only workflow",
      version: 1,
      maxParallelism: 1,
      status: "active",
    },
  ];
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);
  expect(
    screen.queryByRole("button", { name: "Create workflow" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Edit" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("tab", { name: "Examples" }));
  await user.click(
    screen.getByRole("button", {
      name: "Preview Technical incident assessment",
    }),
  );
  expect(screen.getByText(/Deployment app-v42 completed/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Use this example" }),
  ).not.toBeInTheDocument();
});

test("a background detail refresh cannot overwrite an edited workflow draft", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  state.workflows = [copiedWorkflow];
  const user = userEvent.setup();
  const { rerender } = render(<AdminWorkflowsPage />);
  await user.click(screen.getByRole("button", { name: "Edit" }));
  await user.clear(screen.getByLabelText("Workflow name"));
  await user.type(screen.getByLabelText("Workflow name"), "My unsaved edit");
  state.useWorkflow.mockImplementation(() => ({
    data: {
      data: { ...copiedWorkflow, name: "Changed elsewhere", version: 2 },
    },
  }));
  rerender(<AdminWorkflowsPage />);
  expect(screen.getByLabelText("Workflow name")).toHaveValue("My unsaved edit");
});

test("saving a preserved draft uses its original version after a background refresh", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  state.workflows = [copiedWorkflow];
  const user = userEvent.setup();
  const { rerender } = render(<AdminWorkflowsPage />);
  await user.click(screen.getByRole("button", { name: "Edit" }));
  await user.type(screen.getByLabelText("Workflow name"), " edited");
  state.useWorkflow.mockImplementation(() => ({
    data: { data: { ...copiedWorkflow, version: 9 } },
  }));
  rerender(<AdminWorkflowsPage />);
  await user.click(screen.getByRole("button", { name: "4. Check" }));
  await user.click(screen.getByRole("button", { name: "Validate" }));
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(state.replace).toHaveBeenCalledWith(
      expect.objectContaining({ version: 1 }),
    ),
  );
});

test("following a page link also asks before discarding a draft", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  const user = userEvent.setup();
  render(<AdminWorkflowsPage />);
  await user.click(screen.getByRole("button", { name: "Create workflow" }));
  await user.type(screen.getByLabelText("Workflow name"), "Unsaved task");
  const link = document.createElement("a");
  link.href = "/admin/groups";
  link.textContent = "Leave editor";
  link.onclick = (event) => event.preventDefault();
  document.body.appendChild(link);
  try {
    await user.click(link);
    expect(screen.getByRole("alertdialog")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Workflow name")).toHaveValue("Unsaved task");
  } finally {
    link.remove();
  }
});

test("a failed background refetch keeps the dirty editor mounted", async () => {
  state.access = {
    allowed: true,
    canWrite: true,
    isLoading: false,
    error: null,
  };
  state.workflows = [copiedWorkflow];
  const user = userEvent.setup();
  const { rerender } = render(<AdminWorkflowsPage />);
  await user.click(screen.getByRole("button", { name: "Edit" }));
  await user.clear(screen.getByLabelText("Workflow name"));
  await user.type(
    screen.getByLabelText("Workflow name"),
    "Survive network outage",
  );
  state.useWorkflow.mockImplementation(() => ({
    data: { data: copiedWorkflow },
    isError: true,
  }));
  state.useAgentsQuery.mockImplementation(() => ({
    data: { data: [{ id: "agent-1", name: "Agent" }] },
    isError: true,
  }));
  rerender(<AdminWorkflowsPage />);
  expect(screen.getByLabelText("Workflow name")).toHaveValue(
    "Survive network outage",
  );
  expect(screen.getByRole("alert")).toHaveTextContent(/draft is retained/);
});
