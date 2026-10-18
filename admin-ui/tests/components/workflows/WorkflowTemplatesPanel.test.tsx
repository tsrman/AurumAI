import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, expect, test, vi } from "vitest";

import { WorkflowTemplatesPanel } from "@/components/workflows/WorkflowTemplatesPanel";
import type { WorkflowDefinition, WorkflowListItem } from "@/lib/api";
import type { TemplateResponse } from "@/lib/workflow-templates-api";

const hookState = vi.hoisted(() => ({
  templates: { data: undefined as { data: TemplateResponse[] } | undefined, isLoading: false, refetch: vi.fn() },
  selected: { data: undefined as { data: TemplateResponse } | undefined, refetch: vi.fn() },
  source: { data: undefined as { data: WorkflowDefinition } | undefined, isFetching: false, refetch: vi.fn() },
  capture: { mutateAsync: vi.fn(), isPending: false },
  replace: { mutateAsync: vi.fn(), isPending: false },
  instantiate: { mutateAsync: vi.fn(), isPending: false },
  archive: { mutateAsync: vi.fn(), isPending: false },
}));

vi.mock("@/hooks/useWorkflowTemplates", () => ({
  useWorkflowTemplates: () => hookState.templates,
  useWorkflowTemplate: () => hookState.selected,
  useCaptureWorkflowTemplate: () => hookState.capture,
  useReplaceWorkflowTemplate: () => hookState.replace,
  useInstantiateWorkflowTemplate: () => hookState.instantiate,
  useArchiveWorkflowTemplate: () => hookState.archive,
}));

vi.mock("@/hooks/useWorkflows", () => ({
  useWorkflow: () => hookState.source,
}));

const sourceWorkflow: WorkflowDefinition = {
  id: "workflow-1",
  orgId: "org-1",
  name: "Saved workflow",
  description: "Saved description",
  version: 7,
  status: "active",
  supervisorAgentDefinitionId: "agent-supervisor",
  maxParallelism: 2,
  autoRoutable: true,
  nodes: [
    {
      nodeKey: "review",
      displayName: "Review",
      agentDefinitionId: "agent-review",
      instruction: "Saved instruction",
      requiresConfirmation: true,
    },
  ],
  edges: [],
  createdAt: "2026-09-05T00:00:00Z",
  updatedAt: "2026-09-05T00:00:00Z",
};

const template: TemplateResponse = {
  id: "template-1",
  orgId: "org-1",
  name: "Saved template",
  description: "Template description",
  status: "active",
  version: 3,
  createdBy: "user-1",
  updatedBy: "user-1",
  createdAt: "2026-09-05T00:00:00Z",
  updatedAt: "2026-09-05T00:00:00Z",
  snapshot: {
    schemaVersion: 1,
    supervisorAgentKey: "supervisor",
    maxParallelism: 2,
    agents: [],
    nodes: [],
    edges: [],
  },
};

const workflows: WorkflowListItem[] = [
  {
    id: "workflow-1",
    name: "Saved workflow",
    description: "Saved description",
    version: 7,
    maxParallelism: 2,
    status: "active",
    autoRoutable: true,
  },
];

let client: QueryClient;

function Wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function renderPanel(
  onWorkflowCreated = vi.fn(),
  sourceWorkflowId: string | null = "workflow-1"
) {
  return {
    user: userEvent.setup(),
    onWorkflowCreated,
    ...render(
      <WorkflowTemplatesPanel
        workflows={workflows}
        sourceWorkflowId={sourceWorkflowId}
        onWorkflowCreated={onWorkflowCreated}
      />,
      { wrapper: Wrapper }
    ),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  hookState.templates.data = { data: [template] };
  hookState.templates.isLoading = false;
  hookState.selected.data = { data: template };
  hookState.source.data = { data: sourceWorkflow };
  hookState.source.isFetching = false;
  hookState.capture.isPending = false;
  hookState.replace.isPending = false;
  hookState.instantiate.isPending = false;
  hookState.archive.isPending = false;
  hookState.capture.mutateAsync.mockResolvedValue({ data: template });
  hookState.replace.mutateAsync.mockResolvedValue({ data: { ...template, version: 4 } });
  hookState.instantiate.mutateAsync.mockResolvedValue({
    data: { workflow: { ...sourceWorkflow, id: "workflow-copy" }, agentIds: ["agent-copy"] },
  });
  hookState.archive.mutateAsync.mockResolvedValue({
    data: { ...template, status: "archived", version: 4 },
  });
  hookState.templates.refetch.mockResolvedValue(undefined);
  hookState.selected.refetch.mockResolvedValue(undefined);
  hookState.source.refetch.mockResolvedValue(undefined);
});

test("saving captures the fetched saved workflow version rather than editor draft state", async () => {
  const { user } = renderPanel();

  await user.type(screen.getByLabelText("Template name"), "Server snapshot");
  await user.click(screen.getByRole("button", { name: "Save as template" }));

  expect(hookState.capture.mutateAsync).toHaveBeenCalledWith({
    sourceWorkflowId: "workflow-1",
    sourceWorkflowVersion: 7,
    name: "Server snapshot",
    description: "Saved description",
  });
});

test("updating uses the selected active source and expected source/template versions", async () => {
  const { user } = renderPanel();

  expect(screen.getByLabelText("Source workflow")).toHaveValue("workflow-1");
  await user.click(screen.getByRole("button", { name: "Update from workflow" }));

  expect(hookState.replace.mutateAsync).toHaveBeenCalledWith({
    id: "template-1",
    payload: {
      sourceWorkflowId: "workflow-1",
      sourceWorkflowVersion: 7,
      version: 3,
      name: "Saved template",
      description: "Template description",
    },
  });
});

test("instantiation requires a name and opens the independent copy on success", async () => {
  const onWorkflowCreated = vi.fn();
  const { user } = renderPanel(onWorkflowCreated);
  const create = screen.getByRole("button", { name: "Create workflow from template" });

  expect(create).toBeDisabled();
  await user.type(screen.getByLabelText("Workflow copy name"), "Independent copy");
  await user.click(create);

  expect(hookState.instantiate.mutateAsync).toHaveBeenCalledTimes(1);
  expect(hookState.instantiate.mutateAsync).toHaveBeenCalledWith({
    id: "template-1", payload: { version: 3, name: "Independent copy" },
  });
  expect(onWorkflowCreated).toHaveBeenCalledWith("workflow-copy");
  expect(
    screen.getByText(/independent agents.*access rights were not copied.*auto-routing is off/i)
  ).toBeInTheDocument();
});

test("archiving disables further writes for the selected template", async () => {
  const { user } = renderPanel();
  await user.type(screen.getByLabelText("Workflow copy name"), "Copy");
  await user.click(screen.getByRole("button", { name: "Archive template" }));

  expect(hookState.archive.mutateAsync).toHaveBeenCalledWith({ id: "template-1", version: 3 });
  expect(screen.getByRole("button", { name: "Update from workflow" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Create workflow from template" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Archive template" })).toBeDisabled();
});

test("a 409 requires refresh and a separate confirmed mutation", async () => {
  const conflict = Object.assign(new Error("raw secret detail"), {
    response: new Response(JSON.stringify({ error: { code: "version_conflict" } }), {
      status: 409,
      headers: { "content-type": "application/json" },
    }),
  });
  hookState.replace.mutateAsync
    .mockRejectedValueOnce(conflict)
    .mockResolvedValueOnce({ data: { ...template, version: 4 } });
  const { user } = renderPanel();
  const update = screen.getByRole("button", { name: "Update from workflow" });

  await user.click(update);
  expect(await screen.findByText(/the data changed/i)).toBeInTheDocument();
  expect(screen.queryByText("raw secret detail")).not.toBeInTheDocument();
  expect(update).toBeDisabled();
  expect(screen.getByLabelText("Show archived")).toBeDisabled();
  expect(screen.getByLabelText("Workflow template")).toBeDisabled();
  expect(hookState.replace.mutateAsync).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "Refresh data" }));
  expect(hookState.templates.refetch).toHaveBeenCalledTimes(1);
  expect(hookState.selected.refetch).toHaveBeenCalledTimes(1);
  expect(hookState.source.refetch).toHaveBeenCalledTimes(1);
  expect(hookState.replace.mutateAsync).toHaveBeenCalledTimes(1);

  await user.click(update);
  expect(hookState.replace.mutateAsync).toHaveBeenCalledTimes(2);
});

test("a refetch error result keeps conflict writes locked", async () => {
  const conflict = Object.assign(new Error("conflict"), {
    response: new Response(JSON.stringify({ error: { code: "version_conflict" } }), {
      status: 409,
      headers: { "content-type": "application/json" },
    }),
  });
  hookState.replace.mutateAsync.mockRejectedValue(conflict);
  hookState.templates.refetch.mockResolvedValue({ isError: true });
  const { user } = renderPanel();
  const update = screen.getByRole("button", { name: "Update from workflow" });

  await user.click(update);
  await user.click(await screen.findByRole("button", { name: "Refresh data" }));

  expect(await screen.findByText(/could not refresh the data/i)).toBeInTheDocument();
  expect(update).toBeDisabled();
  expect(hookState.replace.mutateAsync).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Refresh data" })).toBeInTheDocument();
});

test("capture conflict refresh never requests empty template detail", async () => {
  const conflict = Object.assign(new Error("conflict"), {
    response: new Response(JSON.stringify({ error: { code: "name_conflict" } }), {
      status: 409,
      headers: { "content-type": "application/json" },
    }),
  });
  hookState.templates.data = { data: [] };
  hookState.selected.data = undefined;
  hookState.capture.mutateAsync.mockRejectedValue(conflict);
  const { user } = renderPanel();

  await user.type(screen.getByLabelText("Template name"), "Conflicting name");
  await user.click(screen.getByRole("button", { name: "Save as template" }));
  await user.click(await screen.findByRole("button", { name: "Refresh data" }));

  expect(hookState.templates.refetch).toHaveBeenCalledTimes(1);
  expect(hookState.selected.refetch).not.toHaveBeenCalled();
  expect(hookState.source.refetch).toHaveBeenCalledTimes(1);
});

test("network loss reports a bounded error and never creates again automatically", async () => {
  hookState.instantiate.mutateAsync.mockRejectedValue(new Error("fetch failed at secret host"));
  const { user } = renderPanel();

  await user.type(screen.getByLabelText("Workflow copy name"), "One attempt");
  await user.click(screen.getByRole("button", { name: "Create workflow from template" }));

  expect(await screen.findByText(/check the connection/i)).toBeInTheDocument();
  expect(screen.getByText(/the operation may already have completed/i)).toBeInTheDocument();
  expect(screen.getByText(/before retrying, refresh and check the.*template.*workflow/i)).toBeInTheDocument();
  await waitFor(() => expect(hookState.instantiate.mutateAsync).toHaveBeenCalledTimes(1));
  expect(screen.queryByText("fetch failed at secret host")).not.toBeInTheDocument();
});

test("explicit unauthorized response asks for login refresh without replaying the write", async () => {
  hookState.archive.mutateAsync.mockRejectedValue(Object.assign(new Error("private auth detail"), {
    response: new Response(JSON.stringify({ error: { code: "UNAUTHORIZED" } }), { status: 401 }),
  }));
  const { user } = renderPanel();
  await user.click(screen.getByRole("button", { name: "Archive template" }));

  expect(await screen.findByText(/session.*sign in again/i)).toBeInTheDocument();
  expect(hookState.archive.mutateAsync).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("private auth detail")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Refresh data" })).not.toBeInTheDocument();
});

test("conflict refresh never requests an empty source workflow", async () => {
  const conflict = Object.assign(new Error("conflict"), {
    response: new Response(JSON.stringify({ error: { code: "version_conflict" } }), {
      status: 409,
      headers: { "content-type": "application/json" },
    }),
  });
  hookState.instantiate.mutateAsync.mockRejectedValue(conflict);
  const { user } = renderPanel(vi.fn(), null);

  await user.type(screen.getByLabelText("Workflow copy name"), "Copy");
  await user.click(screen.getByRole("button", { name: "Create workflow from template" }));
  await user.click(await screen.findByRole("button", { name: "Refresh data" }));

  expect(hookState.templates.refetch).toHaveBeenCalledTimes(1);
  expect(hookState.selected.refetch).toHaveBeenCalledTimes(1);
  expect(hookState.source.refetch).not.toHaveBeenCalled();
});

test("all write controls are disabled while any template action is pending", () => {
  hookState.capture.isPending = true;
  renderPanel();

  expect(screen.getByRole("button", { name: "Save as template" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Update from workflow" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Create workflow from template" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Archive template" })).toBeDisabled();
  expect(screen.getByLabelText("Template name")).toBeDisabled();
  expect(screen.getByLabelText("Show archived")).toBeDisabled();
});

test("source writes wait for a refetching workflow instead of sending a stale version", async () => {
  // Saving the source workflow invalidates its query; until the refetch lands,
  // the cached version is stale and the server would answer 409.
  hookState.source.isFetching = true;
  const { user } = renderPanel();
  await user.type(screen.getByLabelText("Template name"), "Server snapshot");

  expect(screen.getByRole("button", { name: "Update from workflow" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save as template" })).toBeDisabled();
  expect(hookState.replace.mutateAsync).not.toHaveBeenCalled();
  expect(hookState.capture.mutateAsync).not.toHaveBeenCalled();
});
