import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  QueryClient,
  QueryClientProvider,
  type InvalidateQueryFilters,
  type InvalidateOptions,
  type QueryKey,
} from "@tanstack/react-query";
import { useLayoutEffect, useRef, type ReactNode } from "react";
import { useChat } from "../useChat";
import { useAppShell } from "@/hooks/useAppShell";
import * as chatApi from "@/lib/chat-api";
import * as webSourcesApi from "@/lib/web-sources-api";
import * as workflowsApi from "@/lib/workflows-api";
import type { AppShellContextValue } from "@/providers/AppShellProvider";
import type { SSEEvent } from "@/types/chat";

const resumedTurn = vi.hoisted(() => ({ active: false }));

vi.mock("@/hooks/useAppShell", () => ({
  useAppShell: vi.fn(),
}));

vi.mock("@/lib/chat-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/chat-api")>();
  return {
    ...actual,
    createConversation: vi.fn(),
    streamChat: vi.fn(),
    cancelChat: vi.fn(),
    getConversation: vi.fn(),
  };
});

vi.mock("@/lib/workflows-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/workflows-api")>();
  return {
    ...actual,
    cancelWorkflowRun: vi.fn(),
  };
});

vi.mock("@/hooks/useQuestions", () => ({
  questionKeys: {
    list: (conversationId: string) => ["questions", conversationId],
    activeTurn: (conversationId: string) => ["turn", "active", conversationId],
  },
  useQuestions: () => ({
    questions: [],
    activeTurn: null,
    hasActiveTurn: resumedTurn.active,
    isLoading: false,
    error: null,
    answer: vi.fn(),
  }),
}));

const mockUseAppShell = vi.mocked(useAppShell);

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  };
}

function createMockAppShell(
  overrides: Partial<AppShellContextValue> = {}
): AppShellContextValue {
  return {
    sideMini: false,
    toggleSide: vi.fn(),
    agents: [
      {
        id: "general",
        name: "General",
        badge: "G",
        badgeColor: "blue",
        kind: "general",
        kindLabel: "General",
        description: "",
        capabilities: [],
        suggestions: [],
      },
    ],
    myAgents: [],
    activeAgentId: "general",
    setActiveAgentId: vi.fn(),
    addMyAgent: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    activeConversationId: null,
    setActiveConversationId: vi.fn(),
    models: [],
    activeModelId: "gpt-4o-mini",
    setActiveModelId: vi.fn(),
    drawerSource: null,
    setDrawerSource: vi.fn(),
    ...overrides,
  };
}

function enableRealMode() {
  Object.defineProperty(window, "keycloak", {
    value: { token: "fake-token" },
    writable: true,
    configurable: true,
  });
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
}

describe("useChat cache invalidation", () => {
  let queryClient: QueryClient;
  let invalidateQueriesSpy: MockInstance<
    <TTaggedQueryKey extends QueryKey = readonly unknown[]>(
      filters?: InvalidateQueryFilters<TTaggedQueryKey>,
      options?: InvalidateOptions
    ) => Promise<void>
  >;

  beforeEach(() => {
    resumedTurn.active = false;
    vi.resetAllMocks();
    vi.restoreAllMocks();
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    invalidateQueriesSpy = vi.spyOn(queryClient, "invalidateQueries");
    enableRealMode();
    mockUseAppShell.mockReturnValue(createMockAppShell());
  });

  it("hydrates the reopened current conversation final and sources when terminal SSE clears its active turn", async () => {
    resumedTurn.active = true;
    let conversations: AppShellContextValue["conversations"] = [];
    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations = typeof update === "function" ? update(conversations) : update;
    };
    mockUseAppShell.mockReturnValue(createMockAppShell({ activeConversationId: "reopened", setConversations }));
    const conversation = { id: "reopened", title: "Reopened", model: "test", temperature: 0.7, status: "active",
      lastMessageAt: null, createdAt: "2026-09-08T00:00:00Z", messageCount: 0, tokenCount: 0, costUsd: 0 };
    vi.mocked(chatApi.getConversation).mockResolvedValueOnce({ conversation, messages: [] });
    const { rerender } = renderHook(() => useChat(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(conversations).toHaveLength(1));
    const persisted = { id: "final", role: "assistant", content: "Recovered final", model: "test",
      tokensIn: 0, tokensOut: 0, costUsd: 0, latencyMs: null, toolCalls: null, toolResults: null,
      createdAt: "2026-09-08T00:00:00Z", turnId: "resumed", sourceIds: ["source-1"], unsupportedSourceIds: [] };
    vi.mocked(chatApi.getConversation).mockResolvedValue({ conversation, messages: [persisted] });
    const source = { kind: "web" as const, index: 1, sourceId: "source-1", url: "https://example.org/page",
      title: "Page", snippet: "", retrievedAt: "2026-09-08T00:00:00Z", publishedAt: null, truncated: false };
    vi.spyOn(webSourcesApi, "loadSourcesForMessage").mockResolvedValue([source]);
    resumedTurn.active = false;
    rerender();
    await waitFor(() => expect(chatApi.getConversation).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      const final = conversations[0].messages[0];
      expect(final?.role === "assistant" && final.content).toBe("Recovered final");
      expect(final?.role === "assistant" && final.sources).toEqual([source]);
    });
  });

  it("does not let terminal refresh overwrite a next send started before the passive refresh effect", async () => {
    resumedTurn.active = true;
    let conversations: AppShellContextValue["conversations"] = [];
    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations = typeof update === "function" ? update(conversations) : update;
    };
    mockUseAppShell.mockReturnValue(createMockAppShell({ activeConversationId: "reopened", setConversations }));
    const stale = { conversation: { id: "reopened", title: "Reopened", model: "test", temperature: 0.7, status: "active",
      lastMessageAt: null, createdAt: "2026-09-08T00:00:00Z", messageCount: 0, tokenCount: 0, costUsd: 0 }, messages: [] };
    let releaseHistory!: (value: typeof stale) => void;
    const delayedHistory = new Promise<typeof stale>((resolve) => { releaseHistory = resolve; });
    vi.mocked(chatApi.getConversation).mockResolvedValueOnce(stale).mockReturnValue(delayedHistory);
    vi.mocked(chatApi.streamChat).mockReturnValue({ cancel: vi.fn() });
    const { rerender } = renderHook(({ next }) => {
      const chat = useChat();
      const sent = useRef(false);
      useLayoutEffect(() => {
        if (next && !sent.current) {
          sent.current = true;
          chat.send("Next user message");
        }
      }, [next, chat]);
      return chat;
    }, { initialProps: { next: false }, wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(conversations).toHaveLength(1));
    resumedTurn.active = false;
    rerender({ next: true });
    await waitFor(() => expect(chatApi.streamChat).toHaveBeenCalledTimes(1));
    expect(conversations[0].messages.map((message) => message.role)).toEqual(["user", "assistant"]);
    await act(async () => { releaseHistory(stale); await delayedHistory; });
    expect(conversations[0].messages.map((message) => message.role)).toEqual(["user", "assistant"]);
    expect(conversations[0].messages[0].content).toBe("Next user message");
  });

  it.each(["cancel", "unmount"] as const)("does not start a pending send after %s during conversation creation", async (stop) => {
    let releaseCreation!: (value: Awaited<ReturnType<typeof chatApi.createConversation>>) => void;
    const pendingCreation = new Promise<Awaited<ReturnType<typeof chatApi.createConversation>>>((resolve) => { releaseCreation = resolve; });
    vi.mocked(chatApi.createConversation).mockReturnValue(pendingCreation);
    vi.mocked(chatApi.streamChat).mockReturnValue({ cancel: vi.fn() });
    const { result, unmount } = renderHook(() => useChat(), { wrapper: createWrapper(queryClient) });
    act(() => result.current.send("Pending next turn"));
    if (stop === "cancel") act(() => result.current.cancel());
    else unmount();
    await act(async () => {
      releaseCreation({ id: "created", title: "Created", model: "test", temperature: 0.7, status: "active",
        lastMessageAt: null, createdAt: "2026-09-08T00:00:00Z", messageCount: 0, tokenCount: 0, costUsd: 0 });
      await pendingCreation;
    });
    expect(chatApi.streamChat).not.toHaveBeenCalled();
  });

  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    delete (window as { keycloak?: unknown }).keycloak;
    delete process.env.NEXT_PUBLIC_API_URL;
  });

  it("invalidates conversations query after creating a new conversation", async () => {
    vi.mocked(chatApi.createConversation).mockResolvedValue({
      id: "conv-new",
      title: "Hello world",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    });

    vi.mocked(chatApi.streamChat).mockReturnValue({ cancel: vi.fn() });

    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });

    act(() => {
      result.current.send("Hello world");
    });

    await waitFor(() =>
      expect(chatApi.createConversation).toHaveBeenCalledWith({
        title: "Hello world",
        model: "gpt-4o-mini",
      })
    );

    expect(invalidateQueriesSpy).toHaveBeenCalledWith({
      queryKey: ["conversations"],
    });
  });

  it("invalidates conversations query when the stream finishes", async () => {
    vi.mocked(chatApi.createConversation).mockResolvedValue({
      id: "conv-new",
      title: "Hello world",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    });

    let onEventCallback: ((event: SSEEvent) => void) | undefined;

    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      onEventCallback = options.onEvent;
      return { cancel: vi.fn() };
    });

    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });

    act(() => {
      result.current.send("Hello world");
    });

    await waitFor(() => expect(onEventCallback).toBeDefined());

    act(() => {
      onEventCallback?.({ type: "done", data: { turn_id: "turn-1" } });
    });

    expect(invalidateQueriesSpy).toHaveBeenCalledWith({
      queryKey: ["conversations"],
    });
  });

  it("hydrates verified citations when a live turn finishes", async () => {
    let conversations: AppShellContextValue["conversations"] = [];
    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations =
        typeof update === "function" ? update(conversations) : update;
    };
    mockUseAppShell.mockReturnValue(
      createMockAppShell({ conversations, setConversations })
    );
    vi.mocked(chatApi.createConversation).mockResolvedValue({
      id: "conv-new",
      title: "Hello world",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    });
    const persisted = {
      id: "persisted-assistant",
      role: "assistant",
      content: "Verified answer",
      model: "gpt-4o-mini",
      tokensIn: 1,
      tokensOut: 1,
      costUsd: 0,
      latencyMs: null,
      toolCalls: null,
      toolResults: null,
      createdAt: new Date().toISOString(),
      turnId: "turn-web",
      sourceIds: ["source-1"],
      unsupportedSourceIds: [],
    };
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: {
        id: "conv-new",
        title: "Hello world",
        model: "gpt-4o-mini", temperature: 0.7,
        status: "active",
        lastMessageAt: new Date().toISOString(),
        createdAt: new Date().toISOString(),
        messageCount: 2,
        tokenCount: 2,
        costUsd: 0,
      },
      messages: [persisted],
    });
    vi.mocked(chatApi.cancelChat).mockResolvedValue(undefined);
    const source = {
      kind: "web" as const,
      index: 1,
      sourceId: "source-1",
      url: "https://example.org/report",
      title: "Report",
      snippet: "Public report",
      retrievedAt: "2026-09-07T00:00:00Z",
      publishedAt: null,
      truncated: false,
    };
    const hydrate = vi
      .spyOn(webSourcesApi, "loadSourcesForMessage")
      .mockResolvedValue([source]);
    let onEvent: ((event: SSEEvent) => void) | undefined;
    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      onEvent = options.onEvent;
      return { cancel: vi.fn() };
    });

    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });
    act(() => result.current.send("Hello world"));
    await waitFor(() => expect(onEvent).toBeDefined());
    act(() => onEvent?.({ type: "done", data: { turn_id: "turn-web" } }));

    await waitFor(() =>
      expect(hydrate).toHaveBeenCalledWith("conv-new", persisted)
    );
    await waitFor(() => {
      const assistant = conversations.find((conv) => conv.id === "conv-new")
        ?.messages.find((message) => message.role === "assistant");
      expect(assistant?.role === "assistant" ? assistant.sources : []).toEqual([
        source,
      ]);
    });
  });

  it("does not attach delayed sources from an older generation after regenerate", async () => {
    let conversations: AppShellContextValue["conversations"] = [];
    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations =
        typeof update === "function" ? update(conversations) : update;
    };
    const setActiveConversationId = vi.fn();
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        conversations,
        setConversations,
        setActiveConversationId,
      })
    );
    vi.mocked(chatApi.createConversation).mockResolvedValue({
      id: "conv-race",
      title: "Race",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    });
    const persisted = {
      id: "persisted-old",
      role: "assistant",
      content: "Old answer",
      model: "gpt-4o-mini",
      tokensIn: 1,
      tokensOut: 1,
      costUsd: 0,
      latencyMs: null,
      toolCalls: null,
      toolResults: null,
      createdAt: new Date().toISOString(),
      turnId: "turn-old",
      sourceIds: ["source-old"],
      unsupportedSourceIds: [],
    };
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: {
        id: "conv-race",
        title: "Race",
        model: "gpt-4o-mini", temperature: 0.7,
        status: "active",
        lastMessageAt: new Date().toISOString(),
        createdAt: new Date().toISOString(),
        messageCount: 2,
        tokenCount: 2,
        costUsd: 0,
      },
      messages: [persisted],
    });
    let releaseHydration: ((sources: Awaited<ReturnType<typeof webSourcesApi.loadSourcesForMessage>>) => void) | undefined;
    const delayedSources = new Promise<
      Awaited<ReturnType<typeof webSourcesApi.loadSourcesForMessage>>
    >((resolve) => {
      releaseHydration = resolve;
    });
    const hydrate = vi
      .spyOn(webSourcesApi, "loadSourcesForMessage")
      .mockReturnValue(delayedSources);
    const handlers: Array<(event: SSEEvent) => void> = [];
    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      handlers.push(options.onEvent);
      return { cancel: vi.fn() };
    });

    const { result, rerender } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });
    act(() => result.current.send("First request"));
    await waitFor(() => expect(handlers).toHaveLength(1));
    await waitFor(() =>
      expect(conversations.find((conversation) => conversation.id === "conv-race"))
        .toBeDefined()
    );
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        conversations,
        setConversations,
        activeConversationId: "conv-race",
        setActiveConversationId,
      })
    );
    rerender();
    const assistantId = conversations
      .find((conversation) => conversation.id === "conv-race")
      ?.messages.find((message) => message.role === "assistant")?.id;
    expect(assistantId).toBeDefined();

    act(() => handlers[0]({ type: "done", data: { turn_id: "turn-old" } }));
    await waitFor(() =>
      expect(hydrate).toHaveBeenCalledWith("conv-race", persisted)
    );
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        conversations,
        setConversations,
        activeConversationId: "conv-race",
        setActiveConversationId,
      })
    );
    rerender();
    act(() => result.current.regenerate(assistantId!));
    await waitFor(() => expect(handlers).toHaveLength(2));
    releaseHydration?.([
      {
        kind: "web",
        index: 1,
        sourceId: "source-old",
        url: "https://old.example/",
        title: "Old source",
        snippet: null,
        retrievedAt: "2026-09-07T00:00:00Z",
        publishedAt: null,
        truncated: false,
      },
    ]);
    await act(async () => {
      await delayedSources;
    });

    const currentAssistant = conversations
      .find((conversation) => conversation.id === "conv-race")
      ?.messages.find((message) => message.role === "assistant");
    expect(
      currentAssistant?.role === "assistant" ? currentAssistant.sources : []
    ).toEqual([]);
  });

  it("invalidates question recovery queries when the live stream reports a question", async () => {
    vi.mocked(chatApi.createConversation).mockResolvedValue({
      id: "conv-new",
      title: "Hello world",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    });
    let onEventCallback: ((event: SSEEvent) => void) | undefined;
    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      onEventCallback = options.onEvent;
      return { cancel: vi.fn() };
    });
    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });
    act(() => result.current.send("Hello world"));
    await waitFor(() => expect(onEventCallback).toBeDefined());

    act(() => {
      onEventCallback?.({
        type: "question-status",
        data: { questionId: "q1", turnId: "t1", status: "pending", version: 1 },
      } as SSEEvent);
    });

    expect(invalidateQueriesSpy).toHaveBeenCalledWith({
      queryKey: ["questions", "conv-new"],
    });
    expect(invalidateQueriesSpy).toHaveBeenCalledWith({
      queryKey: ["turn", "active", "conv-new"],
    });
  });

  it("rechecks the active turn as soon as stream response headers arrive", async () => {
    vi.mocked(chatApi.createConversation).mockResolvedValue({
      id: "conv-new",
      title: "Hello world",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    });
    let onOpen: (() => void) | undefined;
    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      onOpen = (options as typeof options & { onOpen?: () => void }).onOpen;
      return { cancel: vi.fn() };
    });
    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });

    act(() => result.current.send("Hello world"));
    await waitFor(() => expect(chatApi.streamChat).toHaveBeenCalled());
    expect(onOpen).toBeTypeOf("function");
    act(() => onOpen?.());

    expect(invalidateQueriesSpy).toHaveBeenCalledWith({
      queryKey: ["questions", "conv-new"],
    });
    expect(invalidateQueriesSpy).toHaveBeenCalledWith({
      queryKey: ["turn", "active", "conv-new"],
    });
  });
});

describe("useChat history-reload effect", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    enableRealMode();
  });

  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    delete (window as { keycloak?: unknown }).keycloak;
    delete process.env.NEXT_PUBLIC_API_URL;
  });

  it("does not refetch/clobber messages when only activeAgentId changes", async () => {
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: {
        id: "conv-1",
        title: "Existing chat",
        model: "gpt-4o-mini", temperature: 0.7,
        status: "active",
        lastMessageAt: new Date().toISOString(),
        createdAt: new Date().toISOString(),
        messageCount: 1,
        tokenCount: 0,
        costUsd: 0,
      },
      messages: [],
    });

    // setConversations must be the *same* function reference across
    // rerenders, matching the real AppShellProvider (a useState setter,
    // stable by React's own guarantee) — a fresh vi.fn() per render would
    // itself retrigger the effect and defeat the point of this test.
    const stableSetConversations = vi.fn();
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        activeConversationId: "conv-1",
        activeAgentId: "agent-a",
        setConversations: stableSetConversations,
      })
    );

    const { rerender } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });

    await waitFor(() => expect(chatApi.getConversation).toHaveBeenCalledTimes(1));

    // Switching agents with the same conversation still open must not
    // re-trigger the reload — that used to refetch the persisted (pre-
    // stream) message list and clobber a live SSE stream in progress.
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        activeConversationId: "conv-1",
        activeAgentId: "agent-b",
        setConversations: stableSetConversations,
      })
    );
    rerender();

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(chatApi.getConversation).toHaveBeenCalledTimes(1);
  });

  it("hydrates persisted citations through the owner-only source API", async () => {
    const message = {
      id: "assistant-web",
      role: "assistant",
      content: "Verified answer",
      model: "gpt-4o-mini",
      tokensIn: 1,
      tokensOut: 1,
      costUsd: 0,
      latencyMs: null,
      toolCalls: null,
      toolResults: null,
      createdAt: new Date().toISOString(),
      turnId: "turn-web",
      sourceIds: ["source-1"],
      unsupportedSourceIds: [],
    };
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: {
        id: "conv-web",
        title: "Existing web chat",
        model: "gpt-4o-mini", temperature: 0.7,
        status: "active",
        lastMessageAt: new Date().toISOString(),
        createdAt: new Date().toISOString(),
        messageCount: 1,
        tokenCount: 0,
        costUsd: 0,
      },
      messages: [message],
    });
    const source = {
      kind: "web" as const,
      index: 1,
      sourceId: "source-1",
      url: "https://example.org/report",
      title: "Report",
      snippet: "Public report",
      retrievedAt: "2026-09-07T00:00:00Z",
      publishedAt: null,
      truncated: false,
    };
    const hydrate = vi
      .spyOn(webSourcesApi, "loadSourcesForMessage")
      .mockResolvedValue([source]);
    const setConversations = vi.fn();
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        activeConversationId: "conv-web",
        setConversations,
      })
    );

    renderHook(() => useChat(), { wrapper: createWrapper(queryClient) });

    await waitFor(() => expect(hydrate).toHaveBeenCalledWith("conv-web", message));
    const updater = setConversations.mock.calls.at(-1)?.[0];
    expect(typeof updater).toBe("function");
    const updated = updater([]);
    expect(updated[0].messages[0].sources).toEqual([source]);
  });
});

describe("useChat workflow turns", () => {
  let queryClient: QueryClient;
  let conversations: AppShellContextValue["conversations"];
  let onEvent: ((event: SSEEvent) => void) | undefined;

  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    conversations = [];
    onEvent = undefined;
    enableRealMode();

    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations =
        typeof update === "function" ? update(conversations) : update;
    };
    mockUseAppShell.mockReturnValue(
      createMockAppShell({ conversations, setConversations })
    );
    vi.mocked(chatApi.createConversation).mockResolvedValue({
      id: "conv-workflow",
      title: "Report",
      model: "gpt-4o-mini", temperature: 0.7,
      status: "active",
      lastMessageAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    });
    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      onEvent = options.onEvent;
      return { cancel: vi.fn() };
    });
  });

  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    delete (window as { keycloak?: unknown }).keycloak;
    delete process.env.NEXT_PUBLIC_API_URL;
  });

  it("keeps a completed workflow card when the cleared active turn rehydrates history", async () => {
    resumedTurn.active = false;
    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations = typeof update === "function" ? update(conversations) : update;
    };
    mockUseAppShell.mockReturnValue(
      createMockAppShell({ activeConversationId: "conv-workflow", setConversations })
    );
    const conversation = { id: "conv-workflow", title: "Report", model: "gpt-4o-mini", temperature: 0.7,
      status: "active", lastMessageAt: null, createdAt: "2026-10-11T00:00:00Z", messageCount: 0,
      tokenCount: 0, costUsd: 0 };
    vi.mocked(chatApi.getConversation).mockResolvedValueOnce({ conversation, messages: [] });
    vi.spyOn(webSourcesApi, "loadSourcesForMessage").mockResolvedValue([]);
    const { result, rerender } = renderHook(() => useChat(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(conversations).toHaveLength(1));

    act(() => result.current.send("Build report", "workflow-1"));
    await waitFor(() => expect(onEvent).toBeDefined());
    resumedTurn.active = true;
    rerender();
    const persisted = [
      { id: "persisted-user", role: "user", content: "Build report", model: "gpt-4o-mini", tokensIn: 0,
        tokensOut: 0, costUsd: 0, latencyMs: null, toolCalls: null, toolResults: null,
        createdAt: "2026-10-11T00:00:01Z", turnId: "turn-1", sourceIds: [], unsupportedSourceIds: [] },
      { id: "persisted-final", role: "assistant", content: "Report ready", model: "gpt-4o-mini", tokensIn: 0,
        tokensOut: 0, costUsd: 0, latencyMs: null, toolCalls: null, toolResults: null,
        createdAt: "2026-10-11T00:00:02Z", turnId: "turn-1", sourceIds: [], unsupportedSourceIds: [] },
    ];
    vi.mocked(chatApi.getConversation).mockResolvedValue({ conversation, messages: persisted });
    act(() => {
      onEvent?.({
        type: "workflow-run-status",
        data: { turnId: "turn-1", workflowRunId: "run-1", status: "completed" },
      });
      onEvent?.({ type: "done", data: { turn_id: "turn-1" } });
    });

    // The active-turn query settles only after the live stream has finished.
    resumedTurn.active = false;
    rerender();
    await waitFor(() => {
      const final = conversations[0].messages.find((message) => message.id === "persisted-final");
      expect(final).toMatchObject({ role: "assistant", workflow: { workflowRunId: "run-1", status: "completed" } });
    });
    expect(conversations[0].messages.filter((message) => message.role === "assistant")).toHaveLength(1);
  });

  it("forwards an explicitly selected workflow to streamChat", async () => {
    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });

    act(() => result.current.send("Build report", "workflow-1"));

    await waitFor(() =>
      expect(chatApi.streamChat).toHaveBeenCalledWith(
        expect.objectContaining({ workflowId: "workflow-1" })
      )
    );
  });

  it("reduces safe workflow phases and nodes into the assistant placeholder", async () => {
    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });
    act(() => result.current.send("Build report", "workflow-1"));
    await waitFor(() => expect(onEvent).toBeDefined());

    for (const status of ["planning", "running", "synthesizing"] as const) {
      act(() =>
        onEvent?.({
          type: "workflow-run-status",
          data: { turnId: "turn-1", workflowRunId: "run-1", status },
        })
      );
      const assistant = conversations[0].messages[1];
      expect(assistant.role === "assistant" && assistant.workflow?.status).toBe(status);
    }

    act(() =>
      onEvent?.({
        type: "workflow-node-status",
        data: {
          turnId: "turn-1",
          workflowRunId: "run-1",
          nodeKey: "research",
          nodeName: "Research",
          status: "completed",
        },
      })
    );
    const assistant = conversations[0].messages[1];
    expect(
      assistant.role === "assistant" && assistant.workflow?.nodes.research
    ).toEqual({
      nodeKey: "research",
      nodeName: "Research",
      status: "completed",
    });
  });

  it.each(["failed", "cancelled"] as const)(
    "finalizes an empty placeholder when a workflow is %s",
    async (status) => {
      const { result } = renderHook(() => useChat(), {
        wrapper: createWrapper(queryClient),
      });
      act(() => result.current.send("Build report", "workflow-1"));
      await waitFor(() => expect(onEvent).toBeDefined());

      act(() => {
        onEvent?.({
          type: "workflow-run-status",
          data: {
            turnId: "turn-1",
            workflowRunId: "run-1",
            status,
            ...(status === "failed" ? { errorCode: "access_revoked" } : {}),
          },
        });
        onEvent?.({ type: "done", data: { turn_id: "turn-1" } });
      });

      const assistant = conversations[0].messages[1];
      expect(assistant).toMatchObject({
        role: "assistant",
        content: "",
        contentShown: "",
        phase: "done",
        workflow: { status },
      });
    }
  );

  it("waits for exact-run cancellation acknowledgement before stopping the stream", async () => {
    conversations = [
      {
        id: "conv-workflow",
        agentId: "general",
        title: "Report",
        date: "Today",
        time: "10:00",
        messages: [],
        model: "gpt-4o-mini", temperature: 0.7,
        lastMessageAt: new Date().toISOString(),
        status: "active",
        messageCount: 0,
      },
    ];
    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations = typeof update === "function" ? update(conversations) : update;
    };
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        conversations,
        setConversations,
        activeConversationId: "conv-workflow",
      })
    );
    const abortStream = vi.fn();
    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      onEvent = options.onEvent;
      return { cancel: abortStream };
    });
    let acknowledgeCancel: (() => void) | undefined;
    vi.mocked(workflowsApi.cancelWorkflowRun).mockImplementation(
      () => new Promise<void>((resolve) => {
        acknowledgeCancel = resolve;
      })
    );
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: {
        id: "conv-workflow",
        title: "Report",
        model: "gpt-4o-mini", temperature: 0.7,
        status: "active",
        lastMessageAt: new Date().toISOString(),
        createdAt: new Date().toISOString(),
        messageCount: 0,
        tokenCount: 0,
        costUsd: 0,
      },
      messages: [],
    });
    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });

    await waitFor(() => expect(chatApi.getConversation).toHaveBeenCalled());
    act(() => result.current.send("Build report", "workflow-1"));
    await waitFor(() => expect(onEvent).toBeDefined());
    act(() => {
      onEvent?.({
        type: "workflow-run-status",
        data: { turnId: "turn-1", workflowRunId: "run-1", status: "running" },
      });
    });

    act(() => result.current.cancel());

    await waitFor(() =>
      expect(workflowsApi.cancelWorkflowRun).toHaveBeenCalledWith("run-1")
    );
    expect(chatApi.cancelChat).not.toHaveBeenCalled();
    expect(abortStream).not.toHaveBeenCalled();
    let assistant = conversations[0].messages[1];
    expect(assistant.role === "assistant" && assistant.workflow?.status).toBe("running");

    await act(async () => acknowledgeCancel?.());

    await waitFor(() => {
      assistant = conversations[0].messages[1];
      expect(assistant.role === "assistant" && assistant.workflow?.status).toBe(
        "cancelled"
      );
    });
    expect(abortStream).toHaveBeenCalledOnce();
  });

  it("keeps an exact workflow stream attached when cancellation is rejected", async () => {
    conversations = [
      {
        id: "conv-workflow",
        agentId: "general",
        title: "Report",
        date: "Today",
        time: "10:00",
        messages: [],
        model: "gpt-4o-mini", temperature: 0.7,
        lastMessageAt: new Date().toISOString(),
        status: "active",
        messageCount: 0,
      },
    ];
    const setConversations: AppShellContextValue["setConversations"] = (update) => {
      conversations = typeof update === "function" ? update(conversations) : update;
    };
    mockUseAppShell.mockReturnValue(
      createMockAppShell({
        conversations,
        setConversations,
        activeConversationId: "conv-workflow",
      })
    );
    const abortStream = vi.fn();
    vi.mocked(chatApi.streamChat).mockImplementation((options) => {
      onEvent = options.onEvent;
      return { cancel: abortStream };
    });
    vi.mocked(workflowsApi.cancelWorkflowRun).mockRejectedValue(
      new Error("Workflow cancellation unavailable")
    );
    vi.mocked(chatApi.getConversation).mockResolvedValue({
      conversation: {
        id: "conv-workflow",
        title: "Report",
        model: "gpt-4o-mini", temperature: 0.7,
        status: "active",
        lastMessageAt: new Date().toISOString(),
        createdAt: new Date().toISOString(),
        messageCount: 0,
        tokenCount: 0,
        costUsd: 0,
      },
      messages: [],
    });
    const { result } = renderHook(() => useChat(), {
      wrapper: createWrapper(queryClient),
    });

    await waitFor(() => expect(chatApi.getConversation).toHaveBeenCalled());
    act(() => result.current.send("Build report", "workflow-1"));
    await waitFor(() => expect(onEvent).toBeDefined());
    act(() => {
      onEvent?.({
        type: "workflow-run-status",
        data: { turnId: "turn-1", workflowRunId: "run-1", status: "running" },
      });
      result.current.cancel();
    });

    await waitFor(() => expect(result.current.error).toBe("Workflow cancellation unavailable"));
    const assistant = conversations[0].messages[1];
    expect(assistant.role === "assistant" && assistant.workflow?.status).toBe("running");
    expect(abortStream).not.toHaveBeenCalled();
    expect(result.current.isLoading).toBe(true);
  });
});
