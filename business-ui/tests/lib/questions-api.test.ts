import { waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  answerQuestion,
  getActiveTurn,
  listQuestions,
  subscribeTurnEvents,
} from "@/lib/questions-api";
import type { QuestionResponse, TurnSnapshotResponse } from "@/types/chat";

const question: QuestionResponse = {
  questionId: "question-1",
  turnId: "turn-1",
  text: "Specify the city",
  options: [],
  status: "pending",
  version: 1,
  expiresAt: "2099-01-01T00:00:00Z",
  createdAt: "2026-09-06T10:00:00Z",
  phase: "ordinary",
  nodeKey: null,
  agentName: "Analyst",
  answer: null,
  answeredAt: null,
};

const activeTurn: TurnSnapshotResponse = {
  turnId: "turn-1",
  status: "active",
  version: 1,
  questions: [{ questionId: "question-1", status: "pending", version: 1 }],
};

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify({ data, meta: {} }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(false) };
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("loads questions and the active turn from conversation-scoped endpoints", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(jsonResponse([question]))
    .mockResolvedValueOnce(jsonResponse(activeTurn));
  vi.stubGlobal("fetch", fetchMock);

  await expect(listQuestions("conversation-1")).resolves.toEqual([question]);
  await expect(getActiveTurn("conversation-1")).resolves.toEqual(activeTurn);

  expect((fetchMock.mock.calls[0][0] as Request).url).toContain(
    "/conversations/conversation-1/questions"
  );
  expect((fetchMock.mock.calls[1][0] as Request).url).toContain(
    "/conversations/conversation-1/turns/active"
  );
});

test("submits the answer and idempotency key using the public camelCase contract", async () => {
  let capturedRequest: Request | null = null;
  const fetchMock = vi.fn().mockImplementation((request: Request) => {
    capturedRequest = request.clone();
    return Promise.resolve(jsonResponse({ ...question, status: "answered" }));
  });
  vi.stubGlobal("fetch", fetchMock);

  await answerQuestion(
    "conversation-1",
    "question-1",
    { text: "Berlin" },
    "11111111-1111-4111-8111-111111111111"
  );

  const request = capturedRequest as Request | null;
  expect(request).not.toBeNull();
  if (!request) throw new Error("request was not captured");
  expect(request.method).toBe("POST");
  expect(await request.json()).toEqual({
    answer: { text: "Berlin" },
    idempotencyKey: "11111111-1111-4111-8111-111111111111",
  });
});

test("maps a server-side deadline conflict to an actionable question error", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Interaction state conflict" }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      })
    )
  );

  await expect(
    answerQuestion(
      "conversation-1",
      "question-1",
      { text: "Berlin" },
      "11111111-1111-4111-8111-111111111111"
    )
  ).rejects.toMatchObject({ messageKey: "questionClosed" });
});

test("projects only typed turn and question status events from the stream", async () => {
  const encoder = new TextEncoder();
  const response = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'event: question-status\r\ndata: {"questionId":"question-1","turnId":"turn-1","status":"answered","version":2}\r\n\r\n' +
              'event: private-debug\r\ndata: {"secret":"no"}\r\n\r\n' +
              'event: turn-status\r\ndata: {"turnId":"turn-1","status":"completed","version":3}\r\n\r\n'
          )
        );
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } }
  );
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
  const onEvent = vi.fn();

  subscribeTurnEvents("conversation-1", "turn-1", onEvent, vi.fn());

  await waitFor(() => expect(onEvent).toHaveBeenCalledTimes(2));
  expect(onEvent.mock.calls.map(([event]) => event.type)).toEqual([
    "question-status",
    "turn-status",
  ]);
});
