import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAgentConstructor } from "@/hooks/useAgentConstructor";
import {
  AppShellContext,
  type AppShellContextValue,
} from "@/providers/AppShellProvider";
import type { ConstructorState } from "@/types/aurum";

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  update: vi.fn(),
  knowledge: vi.fn(),
  push: vi.fn(),
  organizationDefault: null as string | null,
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({
    data: {
      data: [
        {
          id: "11111111-1111-4111-8111-111111111111",
          name: "Search",
          type: "tool",
        },
      ],
    },
  }),
}));
vi.mock("@/hooks/useAdminModels", () => ({
  useAdminModels: () => ({ data: { data: { defaultModel: mocks.organizationDefault ?? null } } }),
}));
vi.mock("@/lib/api", () => ({
  createAgentDefinition: (payload: unknown) => mocks.create(payload),
  updateAgentDefinition: (id: string, payload: unknown) => mocks.update(id, payload),
  getKnowledgeBases: () => mocks.knowledge(),
}));

const DEFAULT_STATE: ConstructorState = {
  name: "",
  description: "",
  model: "provider/model-v1",
  prompt: "Be precise",
  temperature: 0.3,
  tools: {},
  bases: {},
};

let initialState: ConstructorState = DEFAULT_STATE;

function Wrapper({ children }: { children: React.ReactNode }) {
  const [constructorState, setConstructorState] =
    React.useState<ConstructorState>(initialState);
  const value = {
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
      <AppShellContext.Provider value={value}>{children}</AppShellContext.Provider>
    </QueryClientProvider>
  );
}

describe("useAgentConstructor", () => {
  beforeEach(() => {
    mocks.create.mockReset();
    mocks.update.mockReset();
    mocks.knowledge.mockReset();
    mocks.push.mockReset();
    mocks.organizationDefault = null;
    initialState = DEFAULT_STATE;
    mocks.knowledge.mockResolvedValue({ data: [], meta: {} });
    mocks.create.mockResolvedValue({ data: { id: "agent-1" }, meta: {} });
  });

  it("updates fields and toggles canonical dependency IDs", () => {
    const { result } = renderHook(() => useAgentConstructor(), { wrapper: Wrapper });

    act(() => {
      result.current.setField("name", "Research");
      result.current.toggleTool("11111111-1111-4111-8111-111111111111");
      result.current.toggleBase("22222222-2222-4222-8222-222222222222");
    });

    expect(result.current.state.name).toBe("Research");
    expect(result.current.state.tools).toEqual({
      "11111111-1111-4111-8111-111111111111": true,
    });
    expect(result.current.state.bases).toEqual({
      "22222222-2222-4222-8222-222222222222": true,
    });
  });

  it("does not persist or redirect when invalid", async () => {
    const { result } = renderHook(() => useAgentConstructor(), { wrapper: Wrapper });

    let saved = true;
    await act(async () => {
      saved = await result.current.save();
    });

    expect(saved).toBe(false);
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("retains the form and exposes the server error when persistence fails", async () => {
    mocks.create.mockRejectedValue(new Error("Dependency access denied"));
    const { result } = renderHook(() => useAgentConstructor(), { wrapper: Wrapper });
    act(() => result.current.setField("name", "Research"));

    await act(async () => {
      expect(await result.current.save()).toBe(false);
    });

    expect(result.current.state.name).toBe("Research");
    expect(result.current.saveError).toBe("Dependency access denied");
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("edits an existing agent without touching the shared create draft", async () => {
    mocks.update.mockResolvedValue({ data: { id: "agent-1" }, meta: {} });
    const agent = {
      id: "agent-1",
      name: "Analyst",
      description: null,
      // No longer in the catalog: an edit may keep it.
      modelId: "provider/retired",
      systemPrompt: "Answer briefly",
      temperature: null,
      skillIds: ["11111111-1111-4111-8111-111111111111"],
      knowledgeBaseIds: [],
      canWrite: true,
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    };
    const { result } = renderHook(
      () => ({ edit: useAgentConstructor(agent), draft: useAgentConstructor() }),
      { wrapper: Wrapper }
    );

    expect(result.current.edit.state).toMatchObject({ name: "Analyst", model: "provider/retired", temperature: null });
    act(() => result.current.edit.setField("name", "Senior analyst"));
    expect(result.current.draft.state.name).toBe("");
    expect(result.current.edit.isValid).toBe(true);

    await act(async () => {
      expect(await result.current.edit.save()).toBe(true);
    });

    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.update).toHaveBeenCalledWith("agent-1", {
      name: "Senior analyst",
      description: "",
      modelId: "provider/retired",
      systemPrompt: "Answer briefly",
      temperature: null,
      skillIds: ["11111111-1111-4111-8111-111111111111"],
      knowledgeBaseIds: [],
    });
    expect(mocks.push).toHaveBeenCalledWith("/admin/agents");
  });

  it("starts a new draft on the organization default, or asks for a model", () => {
    initialState = { ...DEFAULT_STATE, name: "Draft", model: "placeholder" };
    const first = renderHook(() => useAgentConstructor(), { wrapper: Wrapper });
    expect(first.result.current.state.model).toBe("");
    expect(first.result.current.isValid).toBe(false);
    first.unmount();

    mocks.organizationDefault = "provider/model-v1";
    const second = renderHook(() => useAgentConstructor(), { wrapper: Wrapper });
    expect(second.result.current.state.model).toBe("provider/model-v1");
    expect(second.result.current.isValid).toBe(true);
  });
});
