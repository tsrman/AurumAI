import { renderHook, waitFor, act } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  useSkills,
  useCreateSkill,
  useUpdateSkill,
  useDeleteSkill,
  useDiscoverMcpTools,
  useUploadCodeSkill,
  useReplaceCodeSkillBundle,
} from "@/hooks/useSkills";
import type { Skill } from "@/lib/api";

const getSkills = vi.fn();
const createSkill = vi.fn();
const updateSkill = vi.fn();
const deleteSkill = vi.fn();
const discoverMcpTools = vi.fn();
const uploadCodeSkill = vi.fn();
const replaceCodeSkillBundle = vi.fn();

vi.mock("@/lib/api", () => ({
  getSkills: () => getSkills(),
  createSkill: (data: unknown) => createSkill(data),
  updateSkill: (id: string, data: unknown) => updateSkill(id, data),
  deleteSkill: (id: string) => deleteSkill(id),
  discoverMcpTools: (endpoint: string, transport: string, authToken?: string, skillId?: string) =>
    discoverMcpTools(endpoint, transport, authToken, skillId),
  uploadCodeSkill: (form: unknown) => uploadCodeSkill(form),
  replaceCodeSkillBundle: (id: string, form: unknown) => replaceCodeSkillBundle(id, form),
}));

const skill: Skill = {
  id: "skill-1",
  name: "web_search",
  type: "tool",
  inputSchema: {},
};

function Wrapper({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } })
  );
  return React.createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  getSkills.mockReset();
  createSkill.mockReset();
  updateSkill.mockReset();
  deleteSkill.mockReset();
  discoverMcpTools.mockReset();
  uploadCodeSkill.mockReset();
  replaceCodeSkillBundle.mockReset();
});

test("useSkills exposes the fetched skill list", async () => {
  getSkills.mockResolvedValue({ data: [skill], meta: {} });

  const { result } = renderHook(() => useSkills(), { wrapper: Wrapper });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.data).toEqual([skill]);
});

test("useCreateSkill calls createSkill and refetches the list", async () => {
  getSkills.mockResolvedValue({ data: [], meta: {} });
  createSkill.mockResolvedValue({ data: skill, meta: {} });

  const { result } = renderHook(
    () => ({ skills: useSkills(), create: useCreateSkill() }),
    { wrapper: Wrapper }
  );
  await waitFor(() => expect(result.current.skills.isSuccess).toBe(true));

  getSkills.mockResolvedValue({ data: [skill], meta: {} });
  await act(async () => {
    await result.current.create.mutateAsync({ name: "web_search", type: "tool" });
  });

  expect(createSkill).toHaveBeenCalledWith({ name: "web_search", type: "tool" });
  await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));
});

test("useUpdateSkill calls updateSkill with id and payload", async () => {
  updateSkill.mockResolvedValue({ data: { ...skill, name: "renamed" }, meta: {} });
  getSkills.mockResolvedValue({ data: [], meta: {} });

  const { result } = renderHook(() => useUpdateSkill(), { wrapper: Wrapper });
  await act(async () => {
    await result.current.mutateAsync({ id: "skill-1", data: { name: "renamed" } });
  });

  expect(updateSkill).toHaveBeenCalledWith("skill-1", { name: "renamed" });
});

test("useDeleteSkill calls deleteSkill with the id", async () => {
  deleteSkill.mockResolvedValue(undefined);
  getSkills.mockResolvedValue({ data: [], meta: {} });

  const { result } = renderHook(() => useDeleteSkill(), { wrapper: Wrapper });
  await act(async () => {
    await result.current.mutateAsync("skill-1");
  });

  expect(deleteSkill).toHaveBeenCalledWith("skill-1");
});

test("useDiscoverMcpTools calls discoverMcpTools with endpoint and transport", async () => {
  const tools = [{ name: "echo", description: "Echoes back the input string", inputSchema: {} }];
  discoverMcpTools.mockResolvedValue({ data: tools, meta: {} });

  const { result } = renderHook(() => useDiscoverMcpTools(), { wrapper: Wrapper });
  let response: Awaited<ReturnType<typeof result.current.mutateAsync>> | undefined;
  await act(async () => {
    response = await result.current.mutateAsync({
      endpoint: "http://mcp-everything:3001/sse",
      transport: "sse",
    });
  });

  expect(discoverMcpTools).toHaveBeenCalledWith(
    "http://mcp-everything:3001/sse",
    "sse",
    undefined,
    undefined
  );
  expect(response?.data).toEqual(tools);
});

test("useDiscoverMcpTools forwards the auth token when given", async () => {
  discoverMcpTools.mockResolvedValue({ data: [], meta: {} });

  const { result } = renderHook(() => useDiscoverMcpTools(), { wrapper: Wrapper });
  await act(async () => {
    await result.current.mutateAsync({
      endpoint: "http://mcp-fetch:8080/sse",
      transport: "sse",
      authToken: "secret-token",
    });
  });

  expect(discoverMcpTools).toHaveBeenCalledWith(
    "http://mcp-fetch:8080/sse",
    "sse",
    "secret-token",
    undefined
  );
});

test("useDiscoverMcpTools forwards skillId so the backend can fall back to the stored token", async () => {
  discoverMcpTools.mockResolvedValue({ data: [], meta: {} });

  const { result } = renderHook(() => useDiscoverMcpTools(), { wrapper: Wrapper });
  await act(async () => {
    await result.current.mutateAsync({
      endpoint: "http://mcp-fetch:8080/sse",
      transport: "sse",
      skillId: "skill-1",
    });
  });

  expect(discoverMcpTools).toHaveBeenCalledWith(
    "http://mcp-fetch:8080/sse",
    "sse",
    undefined,
    "skill-1"
  );
});

test("useUploadCodeSkill calls uploadCodeSkill and refetches the list", async () => {
  const codeSkill: Skill = { ...skill, id: "skill-2", type: "code" };
  getSkills.mockResolvedValue({ data: [], meta: {} });
  uploadCodeSkill.mockResolvedValue({ data: codeSkill, meta: {} });

  const { result } = renderHook(
    () => ({ skills: useSkills(), upload: useUploadCodeSkill() }),
    { wrapper: Wrapper }
  );
  await waitFor(() => expect(result.current.skills.isSuccess).toBe(true));

  const form = new FormData();
  getSkills.mockResolvedValue({ data: [codeSkill], meta: {} });
  await act(async () => {
    await result.current.upload.mutateAsync(form);
  });

  expect(uploadCodeSkill).toHaveBeenCalledWith(form);
  await waitFor(() => expect(getSkills).toHaveBeenCalledTimes(2));
});

test("useReplaceCodeSkillBundle calls replaceCodeSkillBundle with id and form", async () => {
  const codeSkill: Skill = { ...skill, id: "skill-2", type: "code" };
  getSkills.mockResolvedValue({ data: [], meta: {} });
  replaceCodeSkillBundle.mockResolvedValue({ data: codeSkill, meta: {} });

  const { result } = renderHook(() => useReplaceCodeSkillBundle(), { wrapper: Wrapper });
  const form = new FormData();
  await act(async () => {
    await result.current.mutateAsync({ id: "skill-2", form });
  });

  expect(replaceCodeSkillBundle).toHaveBeenCalledWith("skill-2", form);
});
