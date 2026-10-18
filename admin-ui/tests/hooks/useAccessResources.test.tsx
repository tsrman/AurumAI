import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, expect, test, vi } from "vitest";

import {
  loadAccessResources,
  useAccessResources,
} from "@/hooks/useAccessResources";
import {
  getAdminModels,
  getAdminWorkflows,
  getAgentDefinitions,
  getKnowledgeBases,
  getSkills,
} from "@/lib/api";

vi.mock("@/lib/api", () => ({
  getAdminModels: vi.fn(),
  getSkills: vi.fn(),
  getKnowledgeBases: vi.fn(),
  getAgentDefinitions: vi.fn(),
  getAdminWorkflows: vi.fn(),
}));

beforeEach(() => vi.clearAllMocks());

function Wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

let queryClient: QueryClient;

beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
});

test("normalizes and sorts models", async () => {
  vi.mocked(getAdminModels).mockResolvedValue({
    data: {
      gatewayStatus: "available",
      defaultModel: null,
      defaultAvailable: false,
      models: [
        { id: "provider/z", organizationReadable: false, isDefault: false },
        { id: "provider/a", organizationReadable: true, isDefault: false },
      ],
    },
    meta: { requestId: "request", timestamp: "2026-09-03T00:00:00Z" },
  } as Awaited<ReturnType<typeof getAdminModels>>);

  await expect(loadAccessResources("model")).resolves.toEqual([
    {
      type: "model",
      id: "provider/a",
      label: "provider/a",
      detailKey: "orgReadable",
    },
    {
      type: "model",
      id: "provider/z",
      label: "provider/z",
      detailKey: "orgClosed",
    },
  ]);
});

test("normalizes skills, knowledge bases, agents and archived workflows", async () => {
  vi.mocked(getSkills).mockResolvedValue({
    data: [{ id: "s1", name: "Skill", type: "tool", inputSchema: {} }],
    meta: { requestId: "request", timestamp: "2026-09-03T00:00:00Z" },
  } as Awaited<ReturnType<typeof getSkills>>);
  vi.mocked(getKnowledgeBases).mockResolvedValue({
    data: [
      {
        id: "k1",
        name: "Knowledge",
        description: "Docs",
        canWrite: true,
        createdAt: "",
        updatedAt: "",
      },
    ],
    meta: { requestId: "request", timestamp: "2026-09-03T00:00:00Z" },
  } as Awaited<ReturnType<typeof getKnowledgeBases>>);
  vi.mocked(getAgentDefinitions).mockResolvedValue({
    data: [
      {
        id: "a1",
        name: "Agent",
        description: null,
        modelId: "provider/a",
        systemPrompt: "",
        temperature: 0,
        skillIds: [],
        knowledgeBaseIds: [],
        canWrite: true,
        createdAt: "",
        updatedAt: "",
      },
    ],
    meta: { requestId: "request", timestamp: "2026-09-03T00:00:00Z" },
  } as Awaited<ReturnType<typeof getAgentDefinitions>>);
  vi.mocked(getAdminWorkflows).mockResolvedValue({
    data: [
      {
        id: "w1",
        name: "Workflow",
        description: null,
        version: 2,
        maxParallelism: 1,
        status: "archived",
      },
    ],
    meta: { requestId: "request", timestamp: "2026-09-03T00:00:00Z" },
  } as Awaited<ReturnType<typeof getAdminWorkflows>>);

  await expect(loadAccessResources("skill")).resolves.toEqual([
    { type: "skill", id: "s1", label: "Skill", detail: "tool" },
  ]);
  await expect(loadAccessResources("knowledge_base")).resolves.toEqual([
    {
      type: "knowledge_base",
      id: "k1",
      label: "Knowledge",
      detail: "Docs",
    },
  ]);
  await expect(loadAccessResources("agent_definition")).resolves.toEqual([
    {
      type: "agent_definition",
      id: "a1",
      label: "Agent",
      detail: "provider/a",
    },
  ]);
  await expect(loadAccessResources("workflow")).resolves.toEqual([
    {
      type: "workflow",
      id: "w1",
      label: "Workflow",
      detail: "archived · v2",
    },
  ]);
  expect(getAdminWorkflows).toHaveBeenCalledWith(true);
});

test("does not reuse the incompatible cache owned by another feature", async () => {
  const existingSkillsCache = {
    data: [{ id: "existing", name: "Existing", type: "tool" }],
    meta: {},
  };
  queryClient.setQueryData(["skills"], existingSkillsCache);
  vi.mocked(getSkills).mockResolvedValue({
    data: [{ id: "s1", name: "Skill", type: "tool", inputSchema: {} }],
    meta: { requestId: "request", timestamp: "2026-09-03T00:00:00Z" },
  } as Awaited<ReturnType<typeof getSkills>>);

  const { result } = renderHook(() => useAccessResources("skill"), {
    wrapper: Wrapper,
  });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual([
    { type: "skill", id: "s1", label: "Skill", detail: "tool" },
  ]);
  expect(getSkills).toHaveBeenCalledTimes(1);
  expect(queryClient.getQueryData(["skills"])).toEqual(existingSkillsCache);
});
