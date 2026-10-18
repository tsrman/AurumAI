import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useHistory } from "../useHistory";
import { useAppShell } from "@/hooks/useAppShell";
import { useAuth } from "@/providers/AuthProvider";
import { useConversations } from "@/hooks/useConversations";
import { useUpdateConversation } from "@/hooks/useUpdateConversation";
import { useDeleteConversation } from "@/hooks/useDeleteConversation";
import { useRouter } from "next/navigation";
import * as chatApi from "@/lib/chat-api";
import * as webSourcesApi from "@/lib/web-sources-api";
import type { AppShellContextValue } from "@/providers/AppShellProvider";
import type { ConversationResponse } from "@/types/chat";

vi.mock("@/hooks/useAppShell");
vi.mock("@/providers/AuthProvider");
vi.mock("@/hooks/useConversations");
vi.mock("@/hooks/useUpdateConversation");
vi.mock("@/hooks/useDeleteConversation");
vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
}));
vi.mock("@/lib/chat-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/chat-api")>();
  return {
    ...actual,
    getConversation: vi.fn(),
  };
});
vi.mock("@/lib/web-sources-api", () => ({
  loadSourcesForMessage: vi.fn(),
}));

function createMockQueryResult(
  overrides: Record<string, unknown> = {}
): ReturnType<typeof useConversations> {
  return {
    data: { pages: [{ data: [], pagination: { hasMore: false } }] },
    isLoading: false,
    isError: false,
    error: null,
    hasNextPage: false,
    fetchNextPage: vi.fn(),
    isFetchingNextPage: false,
    refetch: vi.fn(),
    ...overrides,
  } as unknown as ReturnType<typeof useConversations>;
}

function makeDateISO(daysAgo: number): string {
  const now = new Date();
  const startOfToday = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  );
  return new Date(startOfToday.getTime() - daysAgo * 86400000 + 3600000).toISOString();
}

function createConversationResponse(
  id: string,
  daysAgo: number,
  title?: string
): ConversationResponse {
  const date = makeDateISO(daysAgo);
  return {
    id,
    title: title ?? `Conversation ${id}`,
    model: "gpt-4o-mini", temperature: 0.7,
    status: "active",
    lastMessageAt: date,
    createdAt: date,
    messageCount: 1,
    tokenCount: 10,
    costUsd: 0.001,
  };
}

describe("useHistory", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();

    vi.mocked(useConversations).mockReturnValue(createMockQueryResult());
    vi.mocked(useUpdateConversation).mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof useUpdateConversation>);
    vi.mocked(useDeleteConversation).mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof useDeleteConversation>);
    vi.mocked(useAppShell).mockReturnValue({
      setConversations: vi.fn(),
      setActiveConversationId: vi.fn(),
      activeConversationId: null,
    } as unknown as AppShellContextValue);
    vi.mocked(useRouter).mockReturnValue({
      push: vi.fn(),
    } as unknown as ReturnType<typeof useRouter>);
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: true,
    } as unknown as ReturnType<typeof useAuth>);
    vi.mocked(webSourcesApi.loadSourcesForMessage).mockResolvedValue([]);
  });

  it("gates the conversations query on isAuthenticated", () => {
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: false,
    } as unknown as ReturnType<typeof useAuth>);

    renderHook(() => useHistory());

    expect(useConversations).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: false })
    );
  });

  it("reports isLoading while auth hasn't resolved yet, even if the (disabled) query itself isn't loading", () => {
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: false,
    } as unknown as ReturnType<typeof useAuth>);
    vi.mocked(useConversations).mockReturnValue(
      createMockQueryResult({ isLoading: false })
    );

    const { result } = renderHook(() => useHistory());

    expect(result.current.isLoading).toBe(true);
  });

  it("groups conversations by date buckets", () => {
    const data = [
      createConversationResponse("today", 0),
      createConversationResponse("yesterday", 1),
      createConversationResponse("last-week", 3),
      createConversationResponse("last-month", 15),
      createConversationResponse("earlier", 60),
    ];

    vi.mocked(useConversations).mockReturnValue(
      createMockQueryResult({
        data: { pages: [{ data, pagination: { hasMore: false } }] },
        hasNextPage: true,
      })
    );

    const { result } = renderHook(() => useHistory());

    expect(result.current.historyItems).toHaveLength(5);
    expect(result.current.groups).toBe(result.current.historyItems);
    expect(result.current.historyItems.map((group) => group.date)).toEqual([
      "Today",
      "Yesterday",
      "Last 7 days",
      "Last 30 days",
      "Earlier",
    ]);
    expect(result.current.historyItems[0].conversations[0].id).toBe("today");
    expect(result.current.historyItems[1].conversations[0].id).toBe("yesterday");
  });

  it("exposes query state and pagination helpers", () => {
    const fetchNextPage = vi.fn();

    vi.mocked(useConversations).mockReturnValue(
      createMockQueryResult({
        isLoading: true,
        isError: true,
        error: new Error("boom"),
        hasNextPage: true,
        fetchNextPage,
        isFetchingNextPage: true,
      })
    );

    const { result } = renderHook(() => useHistory());

    expect(result.current.isLoading).toBe(true);
    expect(result.current.isError).toBe(true);
    expect(result.current.error).toEqual(new Error("boom"));
    expect(result.current.hasNextPage).toBe(true);
    expect(result.current.isFetchingNextPage).toBe(true);

    result.current.fetchNextPage();
    expect(fetchNextPage).toHaveBeenCalled();
  });

  it("opens a conversation by fetching it, updating shell state and navigating", async () => {
    const setConversations = vi.fn();
    const setActiveConversationId = vi.fn();
    const push = vi.fn();

    vi.mocked(useAppShell).mockReturnValue({
      setConversations,
      setActiveConversationId,
      activeConversationId: null,
    } as unknown as AppShellContextValue);
    vi.mocked(useRouter).mockReturnValue({
      push,
    } as unknown as ReturnType<typeof useRouter>);
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: createConversationResponse("conv-1", 0, "Test chat"),
      messages: [
        {
          id: "msg-1",
          role: "user",
          content: "Hello",
          model: "gpt-4o-mini",
          tokensIn: 1,
          tokensOut: 1,
          costUsd: 0,
          latencyMs: null,
          toolCalls: null,
          toolResults: null,
          createdAt: makeDateISO(0),
        },
      ],
    });

    const { result } = renderHook(() => useHistory());

    await act(async () => {
      await result.current.openConversation("conv-1");
    });

    expect(chatApi.getConversation).toHaveBeenCalledWith("conv-1");
    expect(setConversations).toHaveBeenCalled();
    expect(setActiveConversationId).toHaveBeenCalledWith("conv-1");
    // There is no /chat/[id] route in the app router — the chat page reads
    // the active conversation from AppShellContext, not from the URL.
    expect(push).toHaveBeenCalledWith("/chat");
  });

  it("restores server-verified web sources when opening history", async () => {
    const setConversations = vi.fn();
    vi.mocked(useAppShell).mockReturnValue({
      setConversations,
      setActiveConversationId: vi.fn(),
      activeConversationId: null,
    } as unknown as AppShellContextValue);
    const assistant = {
      id: "msg-web",
      role: "assistant",
      content: "Verified answer",
      model: "gpt-4o-mini",
      tokensIn: 1,
      tokensOut: 1,
      costUsd: 0,
      latencyMs: null,
      toolCalls: null,
      toolResults: null,
      createdAt: makeDateISO(0),
      turnId: "turn-web",
      sourceIds: ["source-1"],
      unsupportedSourceIds: [],
    };
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: createConversationResponse("conv-web", 0, "Web chat"),
      messages: [assistant],
    });
    vi.mocked(webSourcesApi.loadSourcesForMessage).mockResolvedValue([
      {
        kind: "web",
        index: 1,
        sourceId: "source-1",
        url: "https://example.org/report",
        title: "Report",
        snippet: "Public report",
        retrievedAt: "2026-09-07T00:00:00Z",
        publishedAt: null,
        truncated: false,
      },
    ]);

    const { result } = renderHook(() => useHistory());
    await act(async () => {
      await result.current.openConversation("conv-web");
    });

    expect(webSourcesApi.loadSourcesForMessage).toHaveBeenCalledWith(
      "conv-web",
      assistant
    );
    const updater = setConversations.mock.calls.at(-1)?.[0];
    expect(typeof updater).toBe("function");
    const updated = updater([]);
    expect(updated[0].messages[0].sources).toEqual([
      expect.objectContaining({ kind: "web", sourceId: "source-1" }),
    ]);
  });

  it("renames a conversation via the update mutation", () => {
    const mutate = vi.fn();
    vi.mocked(useUpdateConversation).mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useUpdateConversation>);

    const { result } = renderHook(() => useHistory());

    result.current.renameConversation("conv-1", "Renamed chat");

    expect(mutate).toHaveBeenCalledWith({ id: "conv-1", title: "Renamed chat" });
  });

  it("deletes a conversation and clears the active chat when it matches", () => {
    const mutate = vi.fn();
    const setActiveConversationId = vi.fn();

    vi.mocked(useDeleteConversation).mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useDeleteConversation>);
    vi.mocked(useAppShell).mockReturnValue({
      setConversations: vi.fn(),
      setActiveConversationId,
      activeConversationId: "conv-1",
    } as unknown as AppShellContextValue);

    const { result } = renderHook(() => useHistory());

    result.current.deleteConversation("conv-1");

    expect(mutate).toHaveBeenCalledWith("conv-1", expect.any(Object));
    const onSuccess = mutate.mock.calls[0][1].onSuccess;
    onSuccess();
    expect(setActiveConversationId).toHaveBeenCalledWith(null);
  });

  it("deletes a conversation without clearing the active chat when it does not match", () => {
    const mutate = vi.fn();
    const setActiveConversationId = vi.fn();

    vi.mocked(useDeleteConversation).mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useDeleteConversation>);
    vi.mocked(useAppShell).mockReturnValue({
      setConversations: vi.fn(),
      setActiveConversationId,
      activeConversationId: "other-conv",
    } as unknown as AppShellContextValue);

    const { result } = renderHook(() => useHistory());

    result.current.deleteConversation("conv-1");

    expect(mutate).toHaveBeenCalledWith("conv-1", expect.any(Object));
    const onSuccess = mutate.mock.calls[0][1].onSuccess;
    onSuccess();
    expect(setActiveConversationId).not.toHaveBeenCalled();
  });
});
