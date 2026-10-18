import { api } from "./api";
import { HTTPError } from "ky";
import { parseSSEStream } from "./sse";
import type { ApiResponse } from "@/types/api";
import type {
  QuestionAnswer,
  QuestionResponse,
  QuestionStatus,
  TurnSnapshotResponse,
  TurnStatus,
  TurnStreamEvent,
} from "@/types/chat";
import { LocalizedError } from "@/lib/errors";

const QUESTION_STATUSES = new Set<QuestionStatus>([
  "pending",
  "answered",
  "expired",
  "cancelled",
]);
const TURN_STATUSES = new Set<TurnStatus>([
  "active",
  "completed",
  "failed",
  "cancelled",
]);

export async function listQuestions(conversationId: string): Promise<QuestionResponse[]> {
  const response = await api
    .get(`conversations/${conversationId}/questions`)
    .json<ApiResponse<QuestionResponse[]>>();
  return response.data;
}

export async function getActiveTurn(
  conversationId: string
): Promise<TurnSnapshotResponse | null> {
  const response = await api
    .get(`conversations/${conversationId}/turns/active`)
    .json<ApiResponse<TurnSnapshotResponse | null>>();
  return response.data;
}

export async function answerQuestion(
  conversationId: string,
  questionId: string,
  answer: QuestionAnswer,
  idempotencyKey: string
): Promise<QuestionResponse> {
  try {
    const response = await api
      .post(`conversations/${conversationId}/questions/${questionId}/answer`, {
        json: { answer, idempotencyKey },
      })
      .json<ApiResponse<QuestionResponse>>();
    return response.data;
  } catch (cause) {
    const response =
      typeof cause === "object" && cause !== null && "response" in cause
        ? (cause as { response?: Response }).response
        : undefined;
    if (response?.status === 409) {
      throw new LocalizedError("questionClosed", { cause });
    }
    throw cause;
  }
}

function parseTurnEvent(event: string, rawData: string): TurnStreamEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawData);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const value = parsed as Record<string, unknown>;
  if (
    typeof value.turnId !== "string" ||
    typeof value.status !== "string" ||
    typeof value.version !== "number"
  ) {
    return null;
  }
  if (event === "question-status") {
    if (
      typeof value.questionId !== "string" ||
      !QUESTION_STATUSES.has(value.status as QuestionStatus)
    ) {
      return null;
    }
    return {
      type: "question-status",
      data: {
        questionId: value.questionId,
        turnId: value.turnId,
        status: value.status as QuestionStatus,
        version: value.version,
      },
    };
  }
  if (event === "turn-status" && TURN_STATUSES.has(value.status as TurnStatus)) {
    return {
      type: "turn-status",
      data: {
        turnId: value.turnId,
        status: value.status as TurnStatus,
        version: value.version,
      },
    };
  }
  return null;
}

export function subscribeTurnEvents(
  conversationId: string,
  turnId: string,
  onEvent: (event: TurnStreamEvent) => void,
  onError: (error: Error) => void
): { cancel: () => void } {
  const controller = new AbortController();
  let failures = 0;

  const pause = (milliseconds: number) => new Promise<void>((resolve) => {
    if (controller.signal.aborted) return resolve();
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve();
    };
    const timer = window.setTimeout(() => {
      controller.signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    controller.signal.addEventListener("abort", onAbort, { once: true });
  });

  void (async () => {
    while (!controller.signal.aborted) {
      try {
        const response = await api.get(
          `conversations/${conversationId}/turns/${turnId}/events`,
          { signal: controller.signal, timeout: false }
        );
        let terminal = false;
        for await (const message of parseSSEStream(response)) {
          if (controller.signal.aborted) return;
          const event = parseTurnEvent(message.event, message.data);
          if (event) {
            failures = 0;
            onEvent(event);
            if (event.type === "turn-status" && event.data.status !== "active") terminal = true;
          }
        }
        // A terminal snapshot emits turn-status before its question statuses.
        // Drain the snapshot before closing this subscription.
        if (controller.signal.aborted || terminal) return;
        // The backend intentionally closes a still-active reconciliation
        // stream at EVENT_SUBSCRIBE_TIMEOUT. Reconnect until React cancels
        // this exact turn subscription; otherwise a long HITL wait loses
        // every terminal update after the first server-side stream window.
        await pause(250);
      } catch (cause) {
        if (
          controller.signal.aborted ||
          (cause instanceof Error && cause.name === "AbortError")
        ) {
          return;
        }
        onError(cause instanceof Error ? cause : new Error(String(cause)));
        // Permanent HTTP errors must not create an endless reconnect loop.
        // Network failures and temporary server errors resume the same turn;
        // the server sends a fresh durable snapshot on every subscription.
        if (cause instanceof HTTPError && cause.response.status < 500
          && ![408, 429].includes(cause.response.status)) return;
        await pause(Math.min(500 * 2 ** Math.min(failures++, 6), 30_000));
      }
    }
  })();

  return { cancel: () => controller.abort() };
}
