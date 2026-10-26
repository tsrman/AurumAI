import { api } from "./api";
import { parseSSEStream } from "./sse";
import { getRuntimeConfig } from "./runtime-config";
import type { ApiResponse } from "@/types/api";
import type { ToolCall } from "@/types/aurum";
import type {
  ChatMessage,
  ChatRequest,
  ConversationResponse,
  MessageResponse,
  SSEEvent,
} from "@/types/chat";

/**
 * Reconstructs the tool-call/tool-result rows a persisted assistant message
 * carries, matching the shape useChat's live SSE reducer builds ("tool-calls"
 * followed by "tool-result") so history reloads render the same UI.
 */
export function toolCallsFromHistory(
  toolCalls: unknown[] | null,
  toolResults: Record<string, unknown> | null
): ToolCall[] {
  if (!toolCalls || toolCalls.length === 0) return [];
  return toolCalls.map((raw, index) => {
    const call = (raw ?? {}) as {
      call_id?: string;
      skill_id?: string;
      tool?: string;
      label?: string;
      args?: Record<string, unknown>;
    };
    const callId = call.call_id ?? String(index);
    const result = toolResults?.[callId];
    const detail = [
      call.args ? JSON.stringify(call.args) : "",
      result !== undefined ? JSON.stringify(result) : "",
    ]
      .filter(Boolean)
      .join("\n");
    return {
      callId,
      ...(call.tool ? { toolId: call.tool } : {}),
      name: call.label ?? call.skill_id ?? call.tool ?? "unknown",
      detail,
      state: "done" as const,
    };
  });
}

/** Agents a chat turn can run on besides the ordinary chat. */
export type ChatAgentId = "hermes";

export async function getChatAgents(): Promise<{ id: ChatAgentId }[]> {
  const response = await api
    .get("chat/agents")
    .json<ApiResponse<{ id: ChatAgentId }[]>>();
  return response.data;
}

function baseUrl(): string {
  return getRuntimeConfig().apiUrl;
}

export interface StreamChatOptions {
  conversation_id: string;
  messages: ChatMessage[];
  model: string;
  options?: ChatRequest["options"];
  workflowId?: string;
  agent?: ChatAgentId;
  onOpen?: () => void;
  onEvent: (event: SSEEvent) => void;
  onError?: (error: Error) => void;
}

export async function createConversation(payload: {
  title?: string;
  model: string;
  system_prompt?: string;
}): Promise<ConversationResponse> {
  const response = await api
    .post("conversations", { json: payload })
    .json<ApiResponse<ConversationResponse>>();
  return response.data;
}

export async function getConversation(
  id: string
): Promise<{ conversation: ConversationResponse; messages: MessageResponse[] }> {
  type ConversationPage = ConversationResponse & {
    messages: {
      data: MessageResponse[];
      pagination: { hasMore: boolean; cursor?: string | null };
    };
  };
  const messages: MessageResponse[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  while (true) {
    const path = `conversations/${id}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`;
    const response = await api.get(path).json<ApiResponse<ConversationPage>>();
    const { messages: page, ...conversation } = response.data;
    messages.push(...page.data);
    if (!page.pagination.hasMore) return { conversation, messages };
    const nextCursor = page.pagination.cursor;
    if (!nextCursor || seenCursors.has(nextCursor)) {
      throw new Error("Invalid message history cursor");
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
}

export async function listConversations(
  cursor?: string,
  limit: number = 20
): Promise<{
  data: ConversationResponse[];
  pagination: { cursor?: string; hasMore: boolean };
}> {
  const searchParams: Record<string, string> = { limit: String(limit) };
  if (cursor) searchParams.cursor = cursor;
  const response = await api
    .get("conversations", { searchParams })
    .json<ApiResponse<ConversationResponse[]>>();
  return {
    data: response.data,
    pagination: response.pagination ?? { hasMore: false },
  };
}

export async function updateConversation(
  id: string,
  payload: { title?: string; temperature?: number | null }
): Promise<ConversationResponse> {
  const response = await api
    .patch(`conversations/${id}`, { json: payload })
    .json<ApiResponse<ConversationResponse>>();
  return response.data;
}

export async function deleteConversation(id: string): Promise<void> {
  await api.delete(`conversations/${id}`);
}

// A stale window.keycloak.token (e.g. the periodic refresh in
// AuthProvider hasn't run yet — background tabs throttle setInterval)
// would otherwise surface as a raw 401 to the user on the very next
// action after coming back to an idle tab. Both cancelChat and streamChat
// use a plain fetch() (no shared retry logic like api.ts's ky client), so
// they get one self-healing refresh-and-retry attempt of their own.
async function refreshAuthHeader(): Promise<string | undefined> {
  try {
    await window.keycloak?.updateToken(30);
  } catch {
    // Fall through with whatever token we currently have — the retry
    // below will surface the original 401 if it's genuinely invalid.
  }
  return window.keycloak?.token;
}

export async function cancelChat(conversation_id: string): Promise<void> {
  const headers: Record<string, string> = {};
  const token = window.keycloak?.token;
  if (token) headers.Authorization = `Bearer ${token}`;

  let response = await fetch(`${baseUrl()}/chat/${conversation_id}/cancel`, {
    method: "POST",
    headers,
  });

  if (response.status === 401) {
    const refreshedToken = await refreshAuthHeader();
    if (refreshedToken) headers.Authorization = `Bearer ${refreshedToken}`;
    response = await fetch(`${baseUrl()}/chat/${conversation_id}/cancel`, {
      method: "POST",
      headers,
    });
  }

  if (!response.ok) {
    const text = await response.text().catch(() => "Cancel request failed");
    throw new Error(text);
  }
}


export function streamChat(options: StreamChatOptions): { cancel: () => void } {
  const abortController = new AbortController();

  const buildHeaders = (): Record<string, string> => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const token = window.keycloak?.token;
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
  };

  const body = JSON.stringify({
    conversation_id: options.conversation_id,
    messages: options.messages,
    model: options.model,
    options: options.options ?? {},
    ...(options.workflowId ? { workflowId: options.workflowId } : {}),
    ...(options.agent ? { agent: options.agent } : {}),
  });

  const run = async () => {
    try {
      let response = await fetch(`${baseUrl()}/chat/stream`, {
        method: "POST",
        headers: buildHeaders(),
        body,
        signal: abortController.signal,
      });

      if (response.status === 401) {
        await refreshAuthHeader();
        response = await fetch(`${baseUrl()}/chat/stream`, {
          method: "POST",
          headers: buildHeaders(),
          body,
          signal: abortController.signal,
        });
      }

      if (!response.ok) {
        const text = await response.text().catch(() => "Stream request failed");
        throw new Error(text);
      }

      options.onOpen?.();

      let sawTerminalEvent = false;
      for await (const message of parseSSEStream(response)) {
        if (abortController.signal.aborted) break;
        const parsed = parseSSEEvent(message);
        if (!parsed) continue;
        if (parsed.type === "message-end" || parsed.type === "done") {
          sawTerminalEvent = true;
        }
        options.onEvent(parsed);
      }

      // The backend can close the connection without ever sending
      // message-end/done (EVENT_SUBSCRIBE_TIMEOUT, runner crash, proxy
      // drop) — a clean EOF, not a thrown error, so the catch block below
      // never runs. Without this, the UI has no terminal signal at all and
      // stays stuck mid-stream forever.
      if (!abortController.signal.aborted && !sawTerminalEvent) {
        options.onError?.(new Error("Stream ended without a terminal event"));
      }
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") return;
      options.onError?.(
        error instanceof Error ? error : new Error(String(error))
      );
    }
  };

  run();
  return { cancel: () => abortController.abort() };
}

function parseSSEEvent(message: {
  event: string;
  data: string;
}): SSEEvent | null {
  try {
    const parsed: unknown = JSON.parse(message.data);
    if (!parsed || typeof parsed !== "object") return null;
    // Legacy event payloads are already validated by their consumers. New
    // workflow events below are explicitly projected onto an allowlist.
    const data = parsed as never;
    switch (message.event) {
      case "message-start":
        return { type: "message-start", data };
      case "thinking-delta":
        return { type: "thinking-delta", data };
      case "content-delta":
        return { type: "content-delta", data };
      case "tool-calls":
        return { type: "tool-calls", data };
      case "tool-result":
        return { type: "tool-result", data };
      case "run-command":
        return { type: "run-command", data };
      case "run-result":
        return { type: "run-result", data };
      case "message-end":
        return { type: "message-end", data };
      case "done":
        return { type: "done", data };
      case "workflow-run-status": {
        const value = data as Record<string, unknown>;
        const statuses = new Set([
          "pending",
          "planning",
          "running",
          "synthesizing",
          "completed",
          "failed",
          "cancelled",
        ]);
        if (
          typeof value.turnId !== "string" ||
          typeof value.workflowRunId !== "string" ||
          typeof value.status !== "string" ||
          !statuses.has(value.status)
        ) {
          return null;
        }
        return {
          type: "workflow-run-status",
          data: {
            turnId: value.turnId,
            workflowRunId: value.workflowRunId,
            status: value.status as Extract<
              SSEEvent,
              { type: "workflow-run-status" }
            >["data"]["status"],
            ...(typeof value.errorCode === "string"
              ? { errorCode: value.errorCode }
              : {}),
          },
        };
      }
      case "workflow-node-status": {
        const value = data as Record<string, unknown>;
        const statuses = new Set([
          "pending",
          "running",
          "waiting_human",
          "completed",
          "failed",
          "cancelled",
          "skipped",
        ]);
        if (
          typeof value.turnId !== "string" ||
          typeof value.workflowRunId !== "string" ||
          typeof value.nodeKey !== "string" ||
          typeof value.nodeName !== "string" ||
          typeof value.status !== "string" ||
          !statuses.has(value.status)
        ) {
          return null;
        }
        return {
          type: "workflow-node-status",
          data: {
            turnId: value.turnId,
            workflowRunId: value.workflowRunId,
            nodeKey: value.nodeKey,
            nodeName: value.nodeName,
            status: value.status as Extract<
              SSEEvent,
              { type: "workflow-node-status" }
            >["data"]["status"],
            ...(typeof value.errorCode === "string"
              ? { errorCode: value.errorCode }
              : {}),
          },
        };
      }
      case "question-status": {
        const value = data as Record<string, unknown>;
        const statuses = new Set(["pending", "answered", "expired", "cancelled"]);
        if (
          typeof value.questionId !== "string" ||
          typeof value.turnId !== "string" ||
          typeof value.status !== "string" ||
          !statuses.has(value.status) ||
          typeof value.version !== "number"
        ) {
          return null;
        }
        return {
          type: "question-status",
          data: {
            questionId: value.questionId,
            turnId: value.turnId,
            status: value.status as Extract<
              SSEEvent,
              { type: "question-status" }
            >["data"]["status"],
            version: value.version,
          },
        };
      }
      case "turn-status": {
        const value = data as Record<string, unknown>;
        const statuses = new Set(["active", "completed", "failed", "cancelled"]);
        if (
          typeof value.turnId !== "string" ||
          typeof value.status !== "string" ||
          !statuses.has(value.status) ||
          typeof value.version !== "number"
        ) {
          return null;
        }
        return {
          type: "turn-status",
          data: {
            turnId: value.turnId,
            status: value.status as Extract<
              SSEEvent,
              { type: "turn-status" }
            >["data"]["status"],
            version: value.version,
          },
        };
      }
      case "message-score":
        return { type: "message-score", data };
      default:
        return null;
    }
  } catch {
    return null;
  }
}
