import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import { beforeEach, expect, test, vi } from "vitest";

import { useAgentConstructor } from "@/hooks/useAgentConstructor";
import {
  AppShellContext,
  type AppShellContextValue,
} from "@/providers/AppShellProvider";
import type { ConstructorState } from "@/types/aurum";

const mocks = vi.hoisted(() => ({
  createAgentDefinition: vi.fn(),
  getKnowledgeBases: vi.fn(),
  push: vi.fn(),
  organizationDefault: null as string | null,
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/hooks/useAdminModels", () => ({
  useAdminModels: () => ({ data: { data: { defaultModel: mocks.organizationDefault ?? null } } }),
  useOrganizationHermesSettings: () => ({ data: { data: { model: "hermes-model" } } }),
}));
vi.mock("@/lib/api", () => ({
  createAgentDefinition: (payload: unknown) => mocks.createAgentDefinition(payload),
  getKnowledgeBases: () => mocks.getKnowledgeBases(),
}));
vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({
    data: {
      data: [
        {
          id: "11111111-1111-4111-8111-111111111111",
          name: "Web search",
          description: "Searches the web",
          type: "tool",
        },
      ],
    },
  }),
}));

const state: ConstructorState = {
  kind: "agent",
  name: "Research agent",
  description: "Finds evidence",
  model: "provider/model-v1",
  prompt: "Be precise",
  temperature: 0.2,
  tools: { "11111111-1111-4111-8111-111111111111": true },
  bases: { "22222222-2222-4222-8222-222222222222": true },
};

function Wrapper({ children }: { children: React.ReactNode }) {
  const [constructorState, setConstructorState] = React.useState(state);
  const context = {
    models: [
      {
        id: "provider/model-v1",
        name: "Model v1",
        shortName: "Model v1",
        description: "",
      },
    ],
    constructorState,
    setConstructorState,
  } satisfies AppShellContextValue;
  const client = React.useMemo(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    []
  );
  return (
    <QueryClientProvider client={client}>
      <AppShellContext.Provider value={context}>{children}</AppShellContext.Provider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mocks.createAgentDefinition.mockReset();
  mocks.getKnowledgeBases.mockReset();
  mocks.push.mockReset();
  mocks.getKnowledgeBases.mockResolvedValue({
    data: [
      {
        id: "22222222-2222-4222-8222-222222222222",
        name: "Policies",
        description: "Company policies",
        canWrite: false,
        createdAt: "2026-09-01T00:00:00Z",
        updatedAt: "2026-09-01T00:00:00Z",
      },
    ],
    meta: {},
  });
  mocks.createAgentDefinition.mockResolvedValue({ data: { id: "agent-1" }, meta: {} });
});

test("save persists canonical model, skill and knowledge-base IDs", async () => {
  const { result } = renderHook(() => useAgentConstructor(), { wrapper: Wrapper });

  let saved = false;
  await act(async () => {
    saved = await result.current.save();
  });

  expect(saved).toBe(true);
  expect(mocks.createAgentDefinition).toHaveBeenCalledWith({
    name: "Research agent",
    description: "Finds evidence",
    modelId: "provider/model-v1",
    systemPrompt: "Be precise",
    temperature: 0.2,
    skillIds: ["11111111-1111-4111-8111-111111111111"],
    knowledgeBaseIds: ["22222222-2222-4222-8222-222222222222"],
    kind: "agent",
  });
  expect(mocks.push).toHaveBeenCalledWith("/admin/agents");
});

test("a Hermes Agent is saved on the organization's Hermes model without tools", async () => {
  const { result } = renderHook(() => useAgentConstructor(), { wrapper: Wrapper });
  act(() => result.current.setField("kind", "hermes"));

  await act(async () => {
    await result.current.save();
  });

  // The tools chosen before switching the type are not sent.
  expect(mocks.createAgentDefinition).toHaveBeenCalledWith(
    expect.objectContaining({ kind: "hermes", modelId: "hermes-model", skillIds: [], knowledgeBaseIds: [] })
  );
});
