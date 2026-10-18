import {
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, test, vi } from "vitest";

import { useKnowledge } from "@/hooks/useKnowledge";
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  listKnowledgeBases,
} from "@/lib/knowledge-api";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true }),
}));
vi.mock("@/lib/knowledge-api", () => ({
  createKnowledgeBase: vi.fn(),
  deleteKnowledgeBase: vi.fn(),
  listKnowledgeBases: vi.fn(),
}));

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.mocked(listKnowledgeBases).mockReset();
  vi.mocked(createKnowledgeBase).mockReset();
  vi.mocked(deleteKnowledgeBase).mockReset();
});

test("renders only persistent server knowledge bases, never mock cards", async () => {
  vi.mocked(listKnowledgeBases).mockResolvedValue([
    {
      id: "11111111-1111-1111-1111-111111111111",
      name: "Server runbooks",
      description: "Operations",
      canWrite: true,
      createdAt: "2026-09-01T00:00:00Z",
      updatedAt: "2026-09-01T00:00:00Z",
    },
  ]);

  const { result } = renderHook(() => useKnowledge(), { wrapper: Wrapper });

  expect(result.current.knowledgeBases).toEqual([]);
  await waitFor(() => expect(result.current.knowledgeBases).toHaveLength(1));
  expect(result.current.knowledgeBases[0]).toMatchObject({
    id: "11111111-1111-1111-1111-111111111111",
    name: "Server runbooks",
    canWrite: true,
  });
});

test("createBase persists through the API", async () => {
  vi.mocked(listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(createKnowledgeBase).mockResolvedValue({
    id: "11111111-1111-1111-1111-111111111111",
    name: "New base",
    description: "Docs",
    canWrite: true,
    createdAt: "2026-09-01T00:00:00Z",
    updatedAt: "2026-09-01T00:00:00Z",
  });
  const { result } = renderHook(() => useKnowledge(), { wrapper: Wrapper });

  const created = await result.current.createBase(" New base ", " Docs ");

  expect(vi.mocked(createKnowledgeBase).mock.calls[0][0]).toEqual({
    name: "New base",
    description: "Docs",
  });
  expect(created).toMatchObject({
    id: "11111111-1111-1111-1111-111111111111",
    name: "New base",
    canWrite: true,
  });
});

test("createBase rejects a blank canonical name without an API request", async () => {
  vi.mocked(listKnowledgeBases).mockResolvedValue([]);
  const { result } = renderHook(() => useKnowledge(), { wrapper: Wrapper });

  await expect(result.current.createBase("   ", "Docs")).rejects.toMatchObject({
    messageKey: "knowledgeBaseNameRequired",
  });
  expect(createKnowledgeBase).not.toHaveBeenCalled();
});

test("deleteBase removes the canonical id through the API", async () => {
  vi.mocked(listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(deleteKnowledgeBase).mockResolvedValue(undefined);
  const { result } = renderHook(() => useKnowledge(), { wrapper: Wrapper });

  await result.current.deleteBase("kb-1");

  expect(deleteKnowledgeBase).toHaveBeenCalledWith("kb-1");
});
