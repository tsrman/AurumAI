import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";
import {
  createConversation,
  getConversation,
  streamChat,
  toolCallsFromHistory,
} from "@/lib/chat-api";
import type { SSEEvent } from "@/types/chat";

describe("chat-api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("window", { keycloak: { token: "test-token" } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  test("createConversation returns conversation data", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: {
            id: "conv-1",
            title: "New conversation",
            model: "pro",
            status: "active",
            lastMessageAt: null,
            createdAt: "2026-06-19T10:00:00Z",
            messageCount: 0,
            tokenCount: 0,
            costUsd: 0,
          },
          meta: { requestId: "r1", timestamp: "2026-06-19T10:00:00Z" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    const conv = await createConversation({ model: "pro" });
    expect(conv.id).toBe("conv-1");
    expect(conv.model).toBe("pro");
  });

  test("getConversation parses the backend's flat response shape", async () => {
    // Backend spreads conversation fields directly into `data` and adds
    // `messages` as a sibling key (business-backend/src/routers/chat.py
    // get_conversation) — it does NOT nest them under a `conversation` key.
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: {
            id: "conv-1",
            title: "Conversation",
            model: "pro",
            status: "active",
            lastMessageAt: "2026-06-19T10:05:00Z",
            createdAt: "2026-06-19T10:00:00Z",
            messageCount: 1,
            tokenCount: 10,
            costUsd: 0,
            messages: {
              data: [
                {
                  id: "msg-1",
                  role: "user",
                  content: "Hello",
                  model: null,
                  tokensIn: 1,
                  tokensOut: 0,
                  costUsd: 0,
                  latencyMs: null,
                  toolCalls: null,
                  createdAt: "2026-06-19T10:00:00Z",
                },
              ],
              pagination: { hasMore: false },
            },
          },
          meta: { requestId: "r1", timestamp: "2026-06-19T10:00:00Z" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    const { conversation, messages } = await getConversation("conv-1");

    expect(conversation.id).toBe("conv-1");
    expect(conversation.title).toBe("Conversation");
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe("msg-1");
  });

  test("getConversation follows message cursors through all 52 messages", async () => {
    const all = Array.from({ length: 52 }, (_, i) => ({ id: `msg-${i}`, role: "user", content: `${i}` }));
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = new URL((input as Request).url);
      const cursor = url.searchParams.get("cursor");
      if (cursor !== null) expect(cursor).toBe("msg-49");
      return new Response(JSON.stringify({ data: {
        id: "conv-1", title: "Long", model: "test", temperature: 0.7, status: "active",
        createdAt: "2026-01-01T00:00:00Z", lastMessageAt: null,
        messageCount: 52, tokenCount: 0, costUsd: 0,
        messages: { data: cursor ? all.slice(50) : all.slice(0, 50),
          pagination: { hasMore: !cursor, cursor: cursor ? null : "msg-49" } },
      }}), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const { messages } = await getConversation("conv-1");
    expect(messages.map(m => m.id)).toEqual(all.map(m => m.id));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  test.each([null, "same"])("getConversation rejects nonadvancing cursor %s", async cursor => {
    vi.mocked(fetch).mockImplementation(async () => new Response(JSON.stringify({ data: {
      id: "conv-1", messages: { data: [], pagination: { hasMore: true, cursor } },
    }}), { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(getConversation("conv-1")).rejects.toThrow(/cursor/i);
  });

  test("streamChat emits parsed SSE events", async () => {
    const encoder = new TextEncoder();
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                'event: content-delta\ndata: {"delta":"hi","turn_id":"t1"}\n\nevent: done\ndata: {"turn_id":"t1"}\n\n'
              )
            );
            controller.close();
          },
        }),
        { status: 200 }
      )
    );

    const events: unknown[] = [];
    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: (event) => events.push(event),
    });

    await vi.waitFor(() => expect(events).toHaveLength(2));
    expect(events[0]).toEqual({
      type: "content-delta",
      data: { delta: "hi", turn_id: "t1" },
    });
  });

  test("streamChat sends workflowId only when explicitly selected", async () => {
    const response = () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: done\ndata: {"turn_id":"t1"}\n\n'
              )
            );
            controller.close();
          },
        }),
        { status: 200 }
      );
    vi.mocked(fetch)
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(response());

    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "ordinary" }],
      model: "pro",
      onEvent: vi.fn(),
    });
    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "workflow" }],
      model: "pro",
      workflowId: "workflow-1",
      onEvent: vi.fn(),
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body))).toEqual({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "ordinary" }],
      model: "pro",
      options: {},
    });
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[1][1]?.body))).toMatchObject({
      workflowId: "workflow-1",
    });
  });

  test("streamChat sanitizes workflow progress SSE payloads", async () => {
    const encoder = new TextEncoder();
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                'event: workflow-run-status\ndata: {"turnId":"t1","workflowRunId":"r1","status":"planning","rawOutput":"secret"}\n\n' +
                  'event: workflow-node-status\ndata: {"turnId":"t1","workflowRunId":"r1","nodeKey":"research","nodeName":"Research","status":"running","prompt":"secret"}\n\n' +
                  'event: done\ndata: {"turn_id":"t1"}\n\n'
              )
            );
            controller.close();
          },
        }),
        { status: 200 }
      )
    );

    const events: SSEEvent[] = [];
    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: (event) => events.push(event),
    });

    await vi.waitFor(() => expect(events).toHaveLength(3));
    expect(events[0]).toEqual({
      type: "workflow-run-status",
      data: { turnId: "t1", workflowRunId: "r1", status: "planning" },
    });
    expect(events[1]).toEqual({
      type: "workflow-node-status",
      data: {
        turnId: "t1",
        workflowRunId: "r1",
        nodeKey: "research",
        nodeName: "Research",
        status: "running",
      },
    });
  });

  test("streamChat sanitizes question status events", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        'event: question-status\ndata: {"questionId":"q1","turnId":"t1","status":"pending","version":1,"text":"private"}\n\n' +
          'event: done\ndata: {"turn_id":"t1"}\n\n',
        { status: 200 }
      )
    );
    const events: SSEEvent[] = [];

    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: (event) => events.push(event),
    });

    await vi.waitFor(() => expect(events).toHaveLength(2));
    expect(events[0]).toEqual({
      type: "question-status",
      data: {
        questionId: "q1",
        turnId: "t1",
        status: "pending",
        version: 1,
      },
    });
  });

  test.each(["waiting_human", "skipped"])(
    "streamChat delivers %s node progress while filtering private fields and unknown statuses",
    async (status) => {
      const node = { turnId: "t1", workflowRunId: "r1", nodeKey: "alpha", nodeName: "alpha" };
      vi.mocked(fetch).mockResolvedValueOnce(new Response(
        `event: workflow-node-status\ndata: ${JSON.stringify({ ...node, status, prompt: "private" })}\n\n`
        + `event: workflow-node-status\ndata: ${JSON.stringify({ ...node, status: "untrusted-status" })}\n\n`
        + 'event: done\ndata: {"turn_id":"t1"}\n\n',
        { status: 200 },
      ));
      const events: SSEEvent[] = [];
      streamChat({
        conversation_id: "conv-1",
        messages: [{ role: "user", content: "confirm workflow" }],
        model: "pro",
        onEvent: event => events.push(event),
      });
      await vi.waitFor(() => expect(events.some(event => event.type === "done")).toBe(true));
      expect(events).toEqual([
        { type: "workflow-node-status", data: { ...node, status } },
        { type: "done", data: { turn_id: "t1" } },
      ]);
    },
  );

  test("streamChat parses code-skill run-command/run-result events", async () => {
    const encoder = new TextEncoder();
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                'event: run-command\ndata: {"skill_id":"s1","round":1,"command":"ls","turn_id":"t1"}\n\n' +
                  'event: run-result\ndata: {"skill_id":"s1","round":1,"result":{"stdout":"ok","exit_code":0,"timed_out":false},"turn_id":"t1"}\n\n' +
                  'event: done\ndata: {"turn_id":"t1"}\n\n'
              )
            );
            controller.close();
          },
        }),
        { status: 200 }
      )
    );

    const events: unknown[] = [];
    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: (event) => events.push(event),
    });

    await vi.waitFor(() => expect(events).toHaveLength(3));
    expect(events[0]).toEqual({
      type: "run-command",
      data: { skill_id: "s1", round: 1, command: "ls", turn_id: "t1" },
    });
    expect(events[1]).toEqual({
      type: "run-result",
      data: {
        skill_id: "s1",
        round: 1,
        result: { stdout: "ok", exit_code: 0, timed_out: false },
        turn_id: "t1",
      },
    });
  });

  test("streamChat calls onError when the stream closes without message-end/done", async () => {
    const encoder = new TextEncoder();
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        new ReadableStream({
          start(controller) {
            // Backend closed the connection mid-turn (EVENT_SUBSCRIBE_TIMEOUT,
            // runner crash, proxy drop) — a clean EOF, never a terminal event.
            controller.enqueue(
              encoder.encode('event: content-delta\ndata: {"delta":"hi","turn_id":"t1"}\n\n')
            );
            controller.close();
          },
        }),
        { status: 200 }
      )
    );

    const onError = vi.fn();
    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: vi.fn(),
      onError,
    });

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
  });

  test("streamChat does not call onError when aborted", async () => {
    vi.mocked(fetch).mockImplementationOnce(
      (_url: RequestInfo | URL, init?: RequestInit) => {
        const signal = init?.signal;
        const abortError = new Error("Aborted");
        abortError.name = "AbortError";
        return new Promise<never>((_resolve, reject) => {
          if (signal?.aborted) {
            reject(abortError);
            return;
          }
          signal?.addEventListener("abort", () => {
            reject(abortError);
          });
        });
      }
    );

    const onError = vi.fn();
    const { cancel } = streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: vi.fn(),
      onError,
    });

    cancel();

    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(onError).not.toHaveBeenCalled();
  });

  test("streamChat reports non-OK response as error", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500 })
    );

    const onError = vi.fn();
    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: vi.fn(),
      onError,
    });

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
  });

  test("streamChat refreshes a stale token on 401 and retries once", async () => {
    // Regression: AuthProvider's periodic token refresh used to update the
    // underlying Keycloak instance but never wrote the new token back to
    // window.keycloak.token, which streamChat reads directly via a plain
    // fetch() — so a genuinely expired token surfaced as a bare 401 with
    // no attempt to recover, right in the middle of sending a message.
    const updateToken = vi.fn().mockImplementation(async () => {
      (window as unknown as { keycloak: { token: string } }).keycloak.token =
        "refreshed-token";
      return true;
    });
    vi.stubGlobal("window", {
      keycloak: { token: "expired-token", updateToken },
    });

    const encoder = new TextEncoder();
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response("Unauthorized", { status: 401 }))
      .mockResolvedValueOnce(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode('event: done\ndata: {"turn_id":"t1"}\n\n')
              );
              controller.close();
            },
          }),
          { status: 200 }
        )
      );

    const events: unknown[] = [];
    streamChat({
      conversation_id: "conv-1",
      messages: [{ role: "user", content: "hello" }],
      model: "pro",
      onEvent: (event) => events.push(event),
    });

    await vi.waitFor(() => expect(events).toHaveLength(1));
    expect(updateToken).toHaveBeenCalledWith(30);
    expect(fetch).toHaveBeenCalledTimes(2);
    const secondCallHeaders = vi.mocked(fetch).mock.calls[1][1]
      ?.headers as Record<string, string>;
    expect(secondCallHeaders.Authorization).toBe("Bearer refreshed-token");
  });

  test("toolCallsFromHistory reconstructs done tool rows from persisted toolCalls/toolResults", () => {
    const rows = toolCallsFromHistory(
      [{ call_id: "call-1", skill_id: "search", args: { q: "docs" } }],
      { "call-1": { skill_id: "search", output: { found: "yes" } } }
    );

    expect(rows).toEqual([
      {
        callId: "call-1",
        name: "search",
        detail: '{"q":"docs"}\n{"skill_id":"search","output":{"found":"yes"}}',
        state: "done",
      },
    ]);
  });

  test("toolCallsFromHistory preserves safe web labels without model arguments", () => {
    expect(
      toolCallsFromHistory(
        [
          {
            call_id: "web-1",
            tool: "web_search",
            label: "Searching sources",
          },
        ],
        { "web-1": { status: "completed", sourceIds: ["source-1"] } }
      )
    ).toEqual([
      {
        callId: "web-1",
        toolId: "web_search",
        name: "Searching sources",
        detail: JSON.stringify({
          status: "completed",
          sourceIds: ["source-1"],
        }),
        state: "done",
      },
    ]);
  });

  test("toolCallsFromHistory returns an empty array when no tools were called", () => {
    expect(toolCallsFromHistory(null, null)).toEqual([]);
    expect(toolCallsFromHistory([], null)).toEqual([]);
  });

  test("toolCallsFromHistory tolerates a missing result (e.g. a still-pending or dropped tool_results entry)", () => {
    const rows = toolCallsFromHistory(
      [{ call_id: "call-1", skill_id: "search", args: { q: "docs" } }],
      null
    );

    expect(rows).toEqual([
      { callId: "call-1", name: "search", detail: '{"q":"docs"}', state: "done" },
    ]);
  });
});
