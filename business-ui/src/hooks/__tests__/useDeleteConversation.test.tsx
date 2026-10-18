import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useDeleteConversation } from "../useDeleteConversation";
import * as chatApi from "@/lib/chat-api";

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient();
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe("useDeleteConversation", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  it("calls deleteConversation and invalidates cache on success", async () => {
    vi.spyOn(chatApi, "deleteConversation").mockResolvedValue(undefined);

    const { result } = renderHook(() => useDeleteConversation(), { wrapper });
    result.current.mutate("conv-1");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(chatApi.deleteConversation).toHaveBeenCalledWith("conv-1");
  });
});
