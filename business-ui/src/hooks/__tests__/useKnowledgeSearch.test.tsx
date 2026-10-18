import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, test, vi } from "vitest";

import { useKnowledgeSearch } from "@/hooks/useKnowledgeSearch";
import { searchKnowledgeBase } from "@/lib/knowledge-api";

vi.mock("@/lib/knowledge-api", () => ({
  searchKnowledgeBase: vi.fn(),
}));

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.mocked(searchKnowledgeBase).mockReset();
});

test("blank search returns an empty result without an API request", async () => {
  const { result } = renderHook(() => useKnowledgeSearch("kb-1"), {
    wrapper: Wrapper,
  });

  await expect(result.current.search("   ")).resolves.toEqual({
    results: [],
    warnings: [],
  });
  expect(searchKnowledgeBase).not.toHaveBeenCalled();
});

test("search trims the query and uses the fixed UI result limit", async () => {
  const response = {
    results: [
      {
        content: "Owned result",
        relevance_score: 0.25,
        metadata: { document_id: "doc-1", document_title: "runbook" },
      },
    ],
    warnings: [],
  };
  vi.mocked(searchKnowledgeBase).mockResolvedValue(response);
  const { result } = renderHook(() => useKnowledgeSearch("kb-1"), {
    wrapper: Wrapper,
  });

  let received;
  await act(async () => {
    received = await result.current.search("  owned phrase  ");
  });

  expect(received).toEqual(response);
  expect(searchKnowledgeBase).toHaveBeenCalledWith({
    knowledgeBaseId: "kb-1",
    query: "owned phrase",
    limit: 10,
  });
});
