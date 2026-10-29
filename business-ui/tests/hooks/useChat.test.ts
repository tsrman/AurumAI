import { renderHook, act, waitFor } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { useChat } from "@/hooks/useChat";
import type { AssistantMessageView } from "@/types/aurum";
import type { SSEEvent } from "@/types/chat";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

vi.mock("@/lib/chat-api", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  streamChat: vi.fn(),
  cancelChat: vi.fn(),
}));

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true, token: "token" }),
}));

vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));

function Wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(
    QueryClientProvider,
    { client: queryClient },
    React.createElement(AppShellProvider, null, children)
  );
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

test("initial state has no messages and is not streaming", () => {
  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  expect(result.current.messages).toEqual([]);
  expect(result.current.isStreaming).toBe(false);
});

test("send appends user and assistant messages and starts streaming", async () => {
  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("Hello");
  });

  expect(result.current.messages).toHaveLength(2);
  expect(result.current.messages[0].role).toBe("user");
  expect(result.current.messages[1].role).toBe("assistant");
  expect(result.current.isStreaming).toBe(true);

  const assistant = result.current.messages[1] as AssistantMessageView;
  expect(assistant.role).toBe("assistant");
  expect(assistant.phase).toBe("think");
});

test("phases progress and isStreaming updates while streaming", async () => {
  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("Tell me about the platform");
  });

  expect(result.current.isStreaming).toBe(true);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });

  const assistant = result.current.messages[1] as AssistantMessageView;
  expect(assistant.role).toBe("assistant");
  expect(assistant.contentShown.length).toBeGreaterThan(0);
});

test("phase transitions progress think -> tools -> answer -> done", async () => {
  const { result } = renderHook(() => useChat(), {
    wrapper: ({ children }: { children: React.ReactNode }) =>
      React.createElement(
        QueryClientProvider,
        { client: queryClient },
        React.createElement(AppShellProvider, { initialState: { activeAgentId: "sql" } }, children)
      ),
  });

  act(() => {
    result.current.send("SQL: top shifts by downtime");
  });

  const phases: string[] = [];
  let assistant = result.current.messages[1] as AssistantMessageView;
  phases.push(assistant.phase);

  for (let i = 0; i < 200; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    assistant = result.current.messages[1] as AssistantMessageView;
    if (phases[phases.length - 1] !== assistant.phase) {
      phases.push(assistant.phase);
    }
    if (assistant.phase === "done") break;
  }

  expect(phases).toEqual(["think", "tools", "answer", "done"]);
  expect(assistant.tools.every((t) => t.state === "done")).toBe(true);
  expect(assistant.contentShown).toBe(assistant.content);
  expect(assistant.thinkingShown).toBe(assistant.thinking);
});

test("tool states transition from wait -> run -> done", async () => {
  const { result } = renderHook(() => useChat(), {
    wrapper: ({ children }: { children: React.ReactNode }) =>
      React.createElement(
        QueryClientProvider,
        { client: queryClient },
        React.createElement(AppShellProvider, { initialState: { activeAgentId: "sql" } }, children)
      ),
  });

  act(() => {
    result.current.send("SQL: top shifts by downtime");
  });

  const states: string[] = [];
  let assistant = result.current.messages[1] as AssistantMessageView;
  expect(assistant.tools.length).toBeGreaterThan(0);
  states.push(assistant.tools[0].state ?? "wait");

  for (let i = 0; i < 800; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    assistant = result.current.messages[1] as AssistantMessageView;
    const current = assistant.tools[0].state ?? "wait";
    if (states[states.length - 1] !== current) {
      states.push(current);
    }
    if (current === "done") break;
  }

  expect(states).toEqual(["wait", "run", "done"]);
});

test("regenerate resets assistant message and reruns phases", async () => {
  const { result } = renderHook(() => useChat(), {
    wrapper: ({ children }: { children: React.ReactNode }) =>
      React.createElement(
        QueryClientProvider,
        { client: queryClient },
        React.createElement(AppShellProvider, { initialState: { activeAgentId: "rag" } }, children)
      ),
  });

  act(() => {
    result.current.send("SQL: top shifts by downtime");
  });

  await act(async () => {
    await vi.advanceTimersByTimeAsync(20000);
  });

  const assistant = result.current.messages[1] as AssistantMessageView;
  expect(assistant.phase).toBe("done");
  expect(assistant.sources.length).toBeGreaterThan(0);
  const messageId = assistant.id;

  act(() => {
    result.current.regenerate(messageId);
  });

  const reset = result.current.messages[1] as AssistantMessageView;
  expect(reset.phase).toBe("think");
  expect(reset.contentShown).toBe("");
  expect(reset.thinkingShown).toBe("");
  expect(reset.sources).toEqual([]);
  expect(result.current.isStreaming).toBe(true);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(20000);
  });

  const final = result.current.messages[1] as AssistantMessageView;
  expect(final.phase).toBe("done");
  expect(final.contentShown.length).toBeGreaterThan(0);
  expect(final.sources.length).toBeGreaterThan(0);
});

test("isStreaming becomes false after streaming ends", async () => {
  const { result } = renderHook(() => useChat(), {
    wrapper: ({ children }: { children: React.ReactNode }) =>
      React.createElement(
        QueryClientProvider,
        { client: queryClient },
        React.createElement(AppShellProvider, { initialState: { activeAgentId: "sql" } }, children)
      ),
  });

  act(() => {
    result.current.send("SQL: top shifts by downtime");
  });

  expect(result.current.isStreaming).toBe(true);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(20000);
  });

  expect(result.current.isStreaming).toBe(false);
});

test("real mode: creates conversation and streams assistant content", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  vi.mocked(createConversation).mockResolvedValueOnce({
    id: "conv-real",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  vi.mocked(getConversation).mockResolvedValueOnce({
    conversation: {
      id: "conv-real",
      title: "New conversation",
      model: "pro", temperature: 0.7,
      status: "active",
      lastMessageAt: null,
      createdAt: "2026-06-19T10:00:00Z",
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    },
    messages: [],
  });

  let eventHandler: ((event: SSEEvent) => void) | null = null;
  vi.mocked(streamChat).mockImplementationOnce(({ onEvent }) => {
    eventHandler = onEvent;
    return { cancel: vi.fn() };
  });

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("Hello");
  });

  await waitFor(() =>
    expect(result.current.messages.some((m) => m.role === "assistant")).toBe(true)
  );

  // Placeholder shape before the first SSE event lands — AssistantBlock's
  // pending indicator relies on exactly this being phase "tools" with
  // everything else empty. Pin it here so a future refactor of the
  // placeholder can't silently break that condition.
  const pending = result.current.messages.find(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(pending?.phase).toBe("tools");
  expect(pending?.tools).toHaveLength(0);
  expect(pending?.thinking).toBe("");
  expect(pending?.contentShown).toBe("");

  act(() => {
    eventHandler?.({ type: "content-delta", data: { delta: "Hello", turn_id: "t1" } });
    eventHandler?.({ type: "message-end", data: { role: "assistant", content: "Hello", turn_id: "t1" } });
    eventHandler?.({ type: "done", data: { turn_id: "t1" } });
  });

  const assistant = result.current.messages.find(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(assistant).toBeDefined();
  expect(assistant?.contentShown).toBe("Hello");
  expect(assistant?.phase).toBe("done");

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: streamChat's onError finalizes the stuck assistant message", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  vi.mocked(createConversation).mockResolvedValueOnce({
    id: "conv-eof",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  vi.mocked(getConversation).mockResolvedValueOnce({
    conversation: {
      id: "conv-eof",
      title: "New conversation",
      model: "pro", temperature: 0.7,
      status: "active",
      lastMessageAt: null,
      createdAt: "2026-06-19T10:00:00Z",
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    },
    messages: [],
  });

  // Simulates chat-api.ts's own onError firing because the SSE stream
  // closed cleanly without ever sending message-end/done.
  vi.mocked(streamChat).mockImplementationOnce(({ onError }) => {
    onError?.(new Error("Stream ended without a terminal event"));
    return { cancel: vi.fn() };
  });

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("hello");
  });

  await waitFor(() =>
    expect(result.current.messages.some((m) => m.role === "assistant")).toBe(true)
  );
  await waitFor(() => expect(result.current.isStreaming).toBe(false));

  const assistant = result.current.messages.find(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(assistant?.phase).toBe("done");
  expect(result.current.error).toBe("Stream ended without a terminal event");

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: sending while a previous turn is still streaming cancels it on the backend", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const { createConversation, getConversation, streamChat, cancelChat } = await import(
    "@/lib/chat-api"
  );
  vi.mocked(createConversation).mockResolvedValueOnce({
    id: "conv-interrupt",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  vi.mocked(getConversation).mockResolvedValue({
    conversation: {
      id: "conv-interrupt",
      title: "New conversation",
      model: "pro", temperature: 0.7,
      status: "active",
      lastMessageAt: null,
      createdAt: "2026-06-19T10:00:00Z",
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    },
    messages: [],
  });
  vi.mocked(cancelChat).mockResolvedValue(undefined);

  const firstAbort = vi.fn();
  const secondAbort = vi.fn();
  let streamCallCount = 0;
  vi.mocked(streamChat).mockImplementation(() => {
    streamCallCount += 1;
    return { cancel: streamCallCount === 1 ? firstAbort : secondAbort };
  });

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("first message");
  });
  await waitFor(() =>
    expect(result.current.messages.some((m) => m.role === "assistant")).toBe(true)
  );
  // First turn never reaches message-end/done — still "streaming" when the
  // second message is sent.
  expect(result.current.isStreaming).toBe(true);

  act(() => {
    result.current.send("second message, sent mid-stream");
  });
  await waitFor(() => expect(streamCallCount).toBe(2));

  expect(cancelChat).toHaveBeenCalledWith("conv-interrupt");
  // The orphaned first assistant message must be finalized (phase "done"),
  // not left stuck forever — otherwise isStreaming never clears.
  const [firstAssistant] = result.current.messages.filter(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(firstAssistant.phase).toBe("done");

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: a second tool-calls event keeps the earlier card and its result", async () => {
  // The runner publishes one `tool-calls` event per invocation, so a web turn
  // that searches and then reads used to erase the search card and orphan its
  // `tool-result` — while reloading the same turn rendered both.
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const conversation = {
    id: "conv-two-calls",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active" as const,
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  };
  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  vi.mocked(createConversation).mockResolvedValueOnce(conversation);
  vi.mocked(getConversation).mockResolvedValueOnce({ conversation, messages: [] });

  let eventHandler: ((event: SSEEvent) => void) | null = null;
  vi.mocked(streamChat).mockImplementationOnce(({ onEvent }) => {
    eventHandler = onEvent;
    return { cancel: vi.fn() };
  });

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("Find and read");
  });

  await waitFor(() =>
    expect(result.current.messages.some((m) => m.role === "assistant")).toBe(true)
  );

  act(() => {
    eventHandler?.({
      type: "tool-calls",
      data: {
        calls: [{ call_id: "call-search", tool: "web_search", label: "Searching sources" }],
        turn_id: "t1",
      },
    });
    eventHandler?.({
      type: "tool-result",
      data: { call_id: "call-search", tool: "web_search", result: { ok: true }, turn_id: "t1" },
    });
    eventHandler?.({
      type: "tool-calls",
      data: {
        calls: [{ call_id: "call-read", tool: "web_read", label: "Reading page" }],
        turn_id: "t1",
      },
    });
  });

  const assistant = result.current.messages.find(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(assistant?.tools).toHaveLength(2);
  expect(assistant?.tools[0].name).toBe("Searching sources");
  expect(assistant?.tools[0].state).toBe("done");
  expect(assistant?.tools[1].name).toBe("Reading page");
  expect(assistant?.tools[1].state).toBe("run");

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: correlates tool-result by call_id", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  vi.mocked(createConversation).mockResolvedValueOnce({
    id: "conv-real",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  vi.mocked(getConversation).mockResolvedValueOnce({
    conversation: {
      id: "conv-real",
      title: "New conversation",
      model: "pro", temperature: 0.7,
      status: "active",
      lastMessageAt: null,
      createdAt: "2026-06-19T10:00:00Z",
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    },
    messages: [],
  });

  let eventHandler: ((event: SSEEvent) => void) | null = null;
  vi.mocked(streamChat).mockImplementationOnce(({ onEvent }) => {
    eventHandler = onEvent;
    return { cancel: vi.fn() };
  });

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("Hello");
  });

  await waitFor(() =>
    expect(result.current.messages.some((m) => m.role === "assistant")).toBe(true)
  );

  act(() => {
    eventHandler?.({
      type: "tool-calls",
      data: {
        calls: [
          { call_id: "call-1", skill_id: "search", args: { q: "hi" } },
          { call_id: "call-2", skill_id: "search", args: { q: "bye" } },
        ],
        turn_id: "t1",
      },
    });
    eventHandler?.({
      type: "tool-result",
      data: { call_id: "call-2", skill_id: "search", result: { ok: true }, turn_id: "t1" },
    });
  });

  const assistant = result.current.messages.find(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(assistant).toBeDefined();
  expect(assistant?.tools).toHaveLength(2);
  expect(assistant?.tools[0].name).toBe("search");
  expect(assistant?.tools[0].state).toBe("run");
  expect(assistant?.tools[1].state).toBe("done");

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: image tool results attach their ids to the answer", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  vi.mocked(createConversation).mockResolvedValueOnce({
    id: "conv-real",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  vi.mocked(getConversation).mockResolvedValueOnce({
    conversation: {
      id: "conv-real",
      title: "New conversation",
      model: "pro", temperature: 0.7,
      status: "active",
      lastMessageAt: null,
      createdAt: "2026-06-19T10:00:00Z",
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    },
    messages: [],
  });

  let eventHandler: ((event: SSEEvent) => void) | null = null;
  vi.mocked(streamChat).mockImplementationOnce(({ onEvent }) => {
    eventHandler = onEvent;
    return { cancel: vi.fn() };
  });

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("Hello");
  });

  await waitFor(() =>
    expect(result.current.messages.some((m) => m.role === "assistant")).toBe(true)
  );

  act(() => {
    eventHandler?.({
      type: "tool-calls",
      data: {
        calls: [{ call_id: "call-1", tool: "generate_image", label: "Generating image" }],
        turn_id: "t1",
      },
    });
    eventHandler?.({
      type: "tool-result",
      data: {
        call_id: "call-1",
        tool: "generate_image",
        result: {
          status: "completed",
          imageIds: ["55555555-5555-4555-8555-555555555555", "not-an-id"],
        },
        turn_id: "t1",
      },
    });
  });

  const assistant = result.current.messages.find(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(assistant).toBeDefined();
  expect(assistant?.tools[0].name).toBe("Generating image");
  expect(assistant?.tools[0].state).toBe("done");
  expect(assistant?.images).toEqual(["55555555-5555-4555-8555-555555555555"]);

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: attached image ids go with the user message and show on it", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  const conversation = {
    id: "conv-images",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  };
  vi.mocked(createConversation).mockResolvedValueOnce(conversation);
  vi.mocked(getConversation).mockResolvedValueOnce({ conversation, messages: [] });
  vi.mocked(streamChat).mockImplementationOnce(() => ({ cancel: vi.fn() }));
  const imageId = "66666666-6666-4666-8666-666666666666";

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });
  act(() => {
    result.current.send("", undefined, [imageId]);
  });

  await waitFor(() => expect(vi.mocked(streamChat)).toHaveBeenCalled());
  const request = vi.mocked(streamChat).mock.calls.at(-1)![0];
  expect(request.messages).toEqual([{ role: "user", content: "", imageIds: [imageId] }]);
  await waitFor(() =>
    expect(result.current.messages.find((m) => m.role === "user")).toMatchObject({
      content: "",
      images: [imageId],
    })
  );

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: run-command/run-result progress lands in the matching tool row", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };

  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  vi.mocked(createConversation).mockResolvedValueOnce({
    id: "conv-real",
    title: "New conversation",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-06-19T10:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  vi.mocked(getConversation).mockResolvedValueOnce({
    conversation: {
      id: "conv-real",
      title: "New conversation",
      model: "pro", temperature: 0.7,
      status: "active",
      lastMessageAt: null,
      createdAt: "2026-06-19T10:00:00Z",
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    },
    messages: [],
  });

  let eventHandler: ((event: SSEEvent) => void) | null = null;
  vi.mocked(streamChat).mockImplementationOnce(({ onEvent }) => {
    eventHandler = onEvent;
    return { cancel: vi.fn() };
  });

  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => {
    result.current.send("Read the file");
  });

  await waitFor(() =>
    expect(result.current.messages.some((m) => m.role === "assistant")).toBe(true)
  );

  act(() => {
    eventHandler?.({
      type: "tool-calls",
      data: {
        calls: [{ call_id: "call-1", skill_id: "code-skill", args: { task: "read" } }],
        turn_id: "t1",
      },
    });
    eventHandler?.({
      type: "run-command",
      data: { skill_id: "code-skill", round: 1, command: "cat out.txt", turn_id: "t1" },
    });
    eventHandler?.({
      type: "run-result",
      data: {
        skill_id: "code-skill",
        round: 1,
        result: { stdout: "hello", stderr: "", exit_code: 0, timed_out: false },
        turn_id: "t1",
      },
    });
    eventHandler?.({
      type: "run-command",
      data: { skill_id: "code-skill", round: 2, command: "sleep 999", turn_id: "t1" },
    });
    eventHandler?.({
      type: "run-result",
      data: {
        skill_id: "code-skill",
        round: 2,
        result: { stdout: "", stderr: "", exit_code: null, timed_out: true },
        turn_id: "t1",
      },
    });
  });

  const assistant = result.current.messages.find(
    (m): m is AssistantMessageView => m.role === "assistant"
  );
  expect(assistant?.tools).toHaveLength(1);
  expect(assistant?.tools[0].state).toBe("run");
  expect(assistant?.tools[0].detail).toContain("$ cat out.txt → exit code 0");
  expect(assistant?.tools[0].detail).toContain("$ sleep 999 ⏱ the command timed out");

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("real mode: web progress uses only the server-safe label", async () => {
  vi.useRealTimers();

  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };
  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  vi.mocked(createConversation).mockResolvedValueOnce({
    id: "conv-web-progress",
    title: "Web",
    model: "pro", temperature: 0.7,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-09-07T00:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  vi.mocked(getConversation).mockResolvedValueOnce({
    conversation: {
      id: "conv-web-progress",
      title: "Web",
      model: "pro", temperature: 0.7,
      status: "active",
      lastMessageAt: null,
      createdAt: "2026-09-07T00:00:00Z",
      messageCount: 0,
      tokenCount: 0,
      costUsd: 0,
    },
    messages: [],
  });
  let eventHandler: ((event: SSEEvent) => void) | null = null;
  vi.mocked(streamChat).mockImplementationOnce(({ onEvent }) => {
    eventHandler = onEvent;
    return { cancel: vi.fn() };
  });
  const { result } = renderHook(() => useChat(), { wrapper: Wrapper });

  act(() => result.current.send("Find the report"));
  await waitFor(() => expect(eventHandler).not.toBeNull());
  act(() => {
    eventHandler?.({
      type: "tool-calls",
      data: {
        calls: [
          {
            call_id: "web-1",
            tool: "web_search",
            label: "Searching sources",
          },
        ],
        turn_id: "turn-web",
      },
    });
    eventHandler?.({
      type: "tool-result",
      data: {
        call_id: "web-1",
        tool: "web_search",
        label: "Searching sources",
        result: { status: "completed", sourceIds: ["source-1"] },
        turn_id: "turn-web",
      },
    });
  });

  const assistant = result.current.messages.find(
    (message): message is AssistantMessageView => message.role === "assistant"
  );
  expect(assistant?.tools).toEqual([
    expect.objectContaining({
      callId: "web-1",
      name: "Searching sources",
      detail: expect.not.stringContaining("Find the report"),
      state: "done",
    }),
  ]);

  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
});

test("re-rendering with new translator instances does not re-hydrate the conversation", async () => {
  // A server refresh hands the provider new message objects, so translator
  // identity changes. Re-fetching history then would drop client-only workflow
  // progress cards from the conversation.
  vi.useRealTimers();
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };
  const nextIntl = await import("next-intl");
  const { useAppShell } = await import("@/hooks/useAppShell");
  const { getConversation } = await import("@/lib/chat-api");
  const conversation = {
    id: "conv-stable", title: "Stable", model: "pro", temperature: 0.7, status: "active" as const,
    lastMessageAt: null, createdAt: "2026-06-19T10:00:00Z", messageCount: 0, tokenCount: 0, costUsd: 0,
  };
  vi.mocked(getConversation).mockReset();
  vi.mocked(getConversation).mockResolvedValue({ conversation, messages: [] });
  const { default: messages } = await import("../../messages/en.json");
  let fresh: { mockRestore: () => void } | undefined;
  try {
    const { result, rerender } = renderHook(
      () => ({ chat: useChat(), shell: useAppShell() }),
      { wrapper: Wrapper }
    );
    act(() => {
      result.current.shell.setActiveConversationId("conv-stable");
    });
    await waitFor(() => expect(getConversation).toHaveBeenCalled());
    const calls = vi.mocked(getConversation).mock.calls.length;
    fresh = vi.spyOn(nextIntl, "useTranslations").mockImplementation(
      (namespace?: string) =>
        nextIntl.createTranslator({ locale: "en", messages, namespace: namespace as never }) as never
    );
    for (let i = 0; i < 3; i += 1) rerender();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(vi.mocked(getConversation).mock.calls.length).toBe(calls);
  } finally {
    fresh?.mockRestore();
    window.keycloak = originalKeycloak;
  }
});

test("hydrating a conversation keeps the images attached to user messages", async () => {
  vi.useRealTimers();
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };
  const { useAppShell } = await import("@/hooks/useAppShell");
  const { getConversation } = await import("@/lib/chat-api");
  const imageId = "77777777-7777-4777-8777-777777777777";
  const conversation = {
    id: "conv-attached", title: "Attached", model: "pro", temperature: 0.7, status: "active" as const,
    lastMessageAt: null, createdAt: "2026-06-19T10:00:00Z", messageCount: 1, tokenCount: 0, costUsd: 0,
  };
  vi.mocked(getConversation).mockReset();
  vi.mocked(getConversation).mockResolvedValue({
    conversation,
    messages: [
      {
        id: "m-user", role: "user", content: "", model: null, tokensIn: 0, tokensOut: 0,
        costUsd: 0, latencyMs: null, toolCalls: null, toolResults: null,
        createdAt: "2026-06-19T10:00:00Z", imageIds: [imageId, "not-an-id"],
      },
    ],
  });
  try {
    const { result } = renderHook(
      () => ({ chat: useChat(), shell: useAppShell() }),
      { wrapper: Wrapper }
    );
    act(() => {
      result.current.shell.setActiveConversationId("conv-attached");
    });
    await waitFor(() =>
      expect(result.current.chat.messages.find((m) => m.role === "user")).toMatchObject({
        images: [imageId],
      })
    );
  } finally {
    window.keycloak = originalKeycloak;
  }
});

test("hydrating a conversation keeps the agent it runs on", async () => {
  vi.useRealTimers();
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };
  const { useAppShell } = await import("@/hooks/useAppShell");
  const { getConversation } = await import("@/lib/chat-api");
  const conversation = {
    id: "conv-hermes", title: "Hermes", model: "pro", temperature: 0.7, status: "active" as const,
    lastMessageAt: null, createdAt: "2026-06-19T10:00:00Z", messageCount: 0, tokenCount: 0, costUsd: 0,
    agent: "hermes" as const,
  };
  vi.mocked(getConversation).mockReset();
  vi.mocked(getConversation).mockResolvedValue({ conversation, messages: [] });
  try {
    const { result } = renderHook(
      () => ({ chat: useChat(), shell: useAppShell() }),
      { wrapper: Wrapper }
    );
    act(() => {
      result.current.shell.setActiveConversationId("conv-hermes");
    });
    await waitFor(() =>
      expect(result.current.shell.conversations.find((c) => c.id === "conv-hermes")?.agent).toBe("hermes")
    );
  } finally {
    window.keycloak = originalKeycloak;
  }
});

test("a failed turn's reason survives the history reload that follows it", async () => {
  vi.useRealTimers();
  const originalKeycloak = window.keycloak;
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };
  const { useAppShell } = await import("@/hooks/useAppShell");
  const { getConversation } = await import("@/lib/chat-api");
  const conversation = {
    id: "conv-failed", title: "Failed", model: "pro", temperature: 0.7, status: "active" as const,
    lastMessageAt: null, createdAt: "2026-06-19T10:00:00Z", messageCount: 1, tokenCount: 0, costUsd: 0,
  };
  vi.mocked(getConversation).mockReset();
  // The backend keeps the user's message, never the failed turn's reason.
  vi.mocked(getConversation).mockResolvedValue({
    conversation,
    messages: [
      {
        id: "m-user", role: "user", content: "plan a trip", model: null, tokensIn: 0, tokensOut: 0,
        costUsd: 0, latencyMs: null, toolCalls: null, toolResults: null, createdAt: "2026-06-19T10:00:00Z",
      },
    ],
  });
  const reason: AssistantMessageView = {
    id: "local-reason", role: "assistant", agentId: "general", thinking: "", tools: [], widgets: [], sources: [],
    content: "Could not get a model response. Reason: x (hermes_model_unavailable)",
    contentShown: "Could not get a model response. Reason: x (hermes_model_unavailable)",
    thinkingShown: "", phase: "done", thinkingOpen: false, thinkingSeconds: 1, startedAt: Date.now(), failed: true,
  };
  try {
    const { result } = renderHook(() => ({ chat: useChat(), shell: useAppShell() }), { wrapper: Wrapper });
    act(() => {
      result.current.shell.setConversations([
        {
          id: "conv-failed", agentId: "general", title: "Failed", date: "Today", time: "10:00",
          messages: [{ id: "local-user", role: "user", content: "plan a trip" }, reason],
          model: "pro", temperature: 0.7, lastMessageAt: "2026-06-19T10:00:00Z", status: "active", messageCount: 2,
        },
      ]);
      result.current.shell.setActiveConversationId("conv-failed");
    });
    await waitFor(() => expect(getConversation).toHaveBeenCalled());
    await waitFor(() =>
      expect(result.current.chat.messages.map((message) => message.id)).toEqual(["m-user", "local-reason"])
    );
  } finally {
    window.keycloak = originalKeycloak;
  }
});
