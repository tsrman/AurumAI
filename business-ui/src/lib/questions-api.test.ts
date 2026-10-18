import { waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { HTTPError } from "ky";
import { api } from "./api";
import { subscribeTurnEvents } from "./questions-api";

vi.mock("./api", () => ({ api: { get: vi.fn() } }));

beforeEach(() => {
  vi.clearAllMocks();
});

test("recovers status updates after an established stream breaks", async () => {
  const get = vi.mocked(api.get);
  get.mockResolvedValueOnce(new Response(new ReadableStream({
    start(controller) { controller.error(new TypeError("connection reset")); },
  })) as unknown as Awaited<ReturnType<typeof api.get>>);
  get.mockResolvedValueOnce(new Response(
    'event: turn-status\ndata: {"turnId":"turn-1","status":"completed","version":2}\n\n'
  ) as unknown as Awaited<ReturnType<typeof api.get>>);
  const events: unknown[] = [];
  const subscription = subscribeTurnEvents("conversation-1", "turn-1", event => events.push(event), vi.fn());
  try {
    await waitFor(() => expect(events).toEqual([{
      type: "turn-status", data: {turnId: "turn-1", status: "completed", version: 2},
    }]), {timeout: 2000});
  } finally {
    subscription.cancel();
  }
});

test("delivers question states following the terminal turn in the same snapshot", async () => {
  vi.mocked(api.get).mockResolvedValueOnce(new Response(
    'event: turn-status\ndata: {"turnId":"turn-1","status":"cancelled","version":2}\n\n'
    + 'event: question-status\ndata: {"turnId":"turn-1","questionId":"q1","status":"cancelled","version":2}\n\n'
  ) as unknown as Awaited<ReturnType<typeof api.get>>);
  const events: {type: string}[] = [];
  const subscription = subscribeTurnEvents("conversation-1", "turn-1", e => events.push(e), vi.fn());
  try {
    await waitFor(() => expect(events.map(e => e.type)).toEqual(["turn-status", "question-status"]));
  } finally { subscription.cancel(); }
});

test.each([401, 403, 404])("does not reconnect a permanent HTTP %s rejection", async (status) => {
  vi.useFakeTimers();
  const get = vi.mocked(api.get);
  const failure = new HTTPError(new Response(null, {status}), new Request("http://test/events"), {} as never);
  get.mockRejectedValue(failure);
  const onError = vi.fn();
  const subscription = subscribeTurnEvents("conversation-1", "turn-1", vi.fn(), onError);
  try {
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledTimes(1);
  } finally { subscription.cancel(); vi.useRealTimers(); }
});

test("backs off temporary failures and cancellation clears the pending reconnect", async () => {
  vi.useFakeTimers();
  const get = vi.mocked(api.get);
  get.mockRejectedValue(new TypeError("offline"));
  const subscription = subscribeTurnEvents("conversation-1", "turn-1", vi.fn(), vi.fn());
  try {
    await vi.advanceTimersByTimeAsync(499);
    expect(get).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(get).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(999);
    expect(get).toHaveBeenCalledTimes(2);
    subscription.cancel();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(get).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  } finally { subscription.cancel(); vi.useRealTimers(); }
});

test("reconnects when an active turn stream reaches normal server EOF", async () => {
  const get = vi.mocked(api.get);
  get.mockResolvedValueOnce(
    new Response("") as unknown as Awaited<ReturnType<typeof api.get>>
  );
  get.mockImplementationOnce(
    (_url, options) =>
      new Promise((_, reject) => {
        const signal = options?.signal;
        signal?.addEventListener(
          "abort",
          () => reject(new DOMException("cancelled", "AbortError")),
          { once: true }
        );
      }) as ReturnType<typeof api.get>
  );
  const onError = vi.fn();

  const subscription = subscribeTurnEvents(
    "conversation-1",
    "turn-1",
    vi.fn(),
    onError
  );

  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  expect(get).toHaveBeenNthCalledWith(
    2,
    "conversations/conversation-1/turns/turn-1/events",
    expect.objectContaining({ timeout: false })
  );
  subscription.cancel();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(onError).not.toHaveBeenCalled();
});
