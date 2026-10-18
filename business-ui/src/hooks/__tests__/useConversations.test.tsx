import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useConversations } from "../useConversations";
import * as chatApi from "@/lib/chat-api";

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe("useConversations", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  it("returns first page of conversations", async () => {
    vi.spyOn(chatApi, "listConversations").mockResolvedValue({
      data: [
        {
          id: "conv-1",
          title: "First",
          model: "gpt-4o-mini", temperature: 0.7,
          status: "active",
          lastMessageAt: "2026-07-07T10:00:00Z",
          createdAt: "2026-07-07T09:00:00Z",
          messageCount: 5,
          tokenCount: 100,
          costUsd: 0.01,
        },
      ],
      pagination: { hasMore: false },
    });

    const { result } = renderHook(() => useConversations(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.pages[0].data).toHaveLength(1);
    expect(result.current.data?.pages[0].data[0].id).toBe("conv-1");
  });

  it("fetches next page when cursor exists", async () => {
    vi.spyOn(chatApi, "listConversations")
      .mockResolvedValueOnce({
        data: [
          {
            id: "conv-1",
            title: "First",
            model: "gpt-4o-mini", temperature: 0.7,
            status: "active",
            lastMessageAt: "2026-07-07T10:00:00Z",
            createdAt: "2026-07-07T09:00:00Z",
            messageCount: 5,
            tokenCount: 100,
            costUsd: 0.01,
          },
        ],
        pagination: { hasMore: true, cursor: "cursor-1" },
      })
      .mockResolvedValueOnce({
        data: [
          {
            id: "conv-2",
            title: "Second",
            model: "gpt-4o-mini", temperature: 0.7,
            status: "active",
            lastMessageAt: "2026-07-07T11:00:00Z",
            createdAt: "2026-07-07T09:00:00Z",
            messageCount: 3,
            tokenCount: 60,
            costUsd: 0.005,
          },
        ],
        pagination: { hasMore: false },
      });

    const { result } = renderHook(() => useConversations(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(true);
    await result.current.fetchNextPage();

    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));
    expect(result.current.data?.pages[1].data[0].id).toBe("conv-2");
  });
});
