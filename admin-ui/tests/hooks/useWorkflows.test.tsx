import * as React from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";

import {
  useReplaceWorkflow,
  useWorkflows,
  workflowKeys,
} from "@/hooks/useWorkflows";
import type { WorkflowDraft } from "@/lib/api";

const getAdminWorkflows = vi.fn();
const replaceWorkflow = vi.fn();
vi.mock("@/lib/api", () => ({
  getAdminWorkflows: (includeArchived: boolean) => getAdminWorkflows(includeArchived),
  replaceWorkflow: (id: string, version: number, draft: WorkflowDraft) =>
    replaceWorkflow(id, version, draft),
  archiveWorkflow: vi.fn(),
  createWorkflow: vi.fn(),
  getAdminWorkflow: vi.fn(),
  getWorkflowDiagnostic: vi.fn(),
  validateWorkflow: vi.fn(),
}));

function Wrapper({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } })
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  getAdminWorkflows.mockReset();
  replaceWorkflow.mockReset();
});

test("keys include workflow identity/version and replacement invalidates lists", async () => {
  expect(workflowKeys.detail("workflow-1", 7)).toEqual([
    "admin-workflows",
    "detail",
    "workflow-1",
    7,
  ]);
  getAdminWorkflows.mockResolvedValue({ data: [], meta: {} });
  replaceWorkflow.mockResolvedValue({ data: {}, meta: {} });
  const draft = {
    name: "Flow",
    supervisorAgentDefinitionId: "agent-1",
    maxParallelism: 1,
    nodes: [],
    edges: [],
  } satisfies WorkflowDraft;

  const { result } = renderHook(
    () => ({ list: useWorkflows(), replace: useReplaceWorkflow() }),
    { wrapper: Wrapper }
  );
  await waitFor(() => expect(result.current.list.isSuccess).toBe(true));
  await act(async () => {
    await result.current.replace.mutateAsync({ id: "workflow-1", version: 7, draft });
  });

  expect(replaceWorkflow).toHaveBeenCalledWith("workflow-1", 7, draft);
  await waitFor(() => expect(getAdminWorkflows).toHaveBeenCalledTimes(2));
});
