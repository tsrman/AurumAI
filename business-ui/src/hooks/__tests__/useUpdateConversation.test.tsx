import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useUpdateConversation } from "../useUpdateConversation";
import * as chatApi from "@/lib/chat-api";

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient();
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe("useUpdateConversation", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  it("calls updateConversation and invalidates cache on success", async () => {
    vi.spyOn(chatApi, "updateConversation").mockResolvedValue({
      id: "conv-1",
      title: "Updated",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: "2026-07-07T10:00:00Z",
      createdAt: "2026-07-07T09:00:00Z",
      messageCount: 5,
      tokenCount: 100,
      costUsd: 0.01,
    });

    const { result } = renderHook(() => useUpdateConversation(), { wrapper });
    result.current.mutate({ id: "conv-1", title: "Updated" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(chatApi.updateConversation).toHaveBeenCalledWith("conv-1", { title: "Updated" });
  });

  it("also accepts a temperature-only update, including null to clear an override", async () => {
    vi.spyOn(chatApi, "updateConversation").mockResolvedValue({
      id: "conv-1",
      title: "Updated",
      model: "gpt-4o-mini",
      temperature: 0.9,
      status: "active",
      lastMessageAt: "2026-07-07T10:00:00Z",
      createdAt: "2026-07-07T09:00:00Z",
      messageCount: 5,
      tokenCount: 100,
      costUsd: 0.01,
    });

    const { result } = renderHook(() => useUpdateConversation(), { wrapper });
    result.current.mutate({ id: "conv-1", temperature: 0.9 });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(chatApi.updateConversation).toHaveBeenCalledWith("conv-1", {
      temperature: 0.9,
    });

    result.current.mutate({ id: "conv-1", temperature: null });
    await waitFor(() =>
      expect(chatApi.updateConversation).toHaveBeenLastCalledWith("conv-1", {
        temperature: null,
      })
    );
  });
});
