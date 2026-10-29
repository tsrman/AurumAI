"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAppShell } from "@/hooks/useAppShell";
import { getRuntimeConfig } from "@/lib/runtime-config";
import {
  cancelChat,
  createConversation as createBackendConversation,
  getConversation as getBackendConversation,
  streamChat,
  toolCallsFromHistory,
  type ChatAgentId,
} from "@/lib/chat-api";
import { cancelWorkflowRun } from "@/lib/workflows-api";
import { loadSourcesForMessage } from "@/lib/web-sources-api";
import { imageIdsFrom } from "@/lib/images-api";
import { questionKeys, useQuestions } from "@/hooks/useQuestions";
import {
  generalResp,
  analystResp,
  ragResp,
  sqlResp,
  swarmResp,
  customResp,
} from "@/lib/mock-data";
import type {
  Agent,
  AssistantMessage,
  AssistantMessageView,
  Conversation,
  Message,
  Source,
  UserMessage,
} from "@/types/aurum";
import type {
  MessageResponse,
  QuestionAnswer,
  QuestionResponse,
  SSEEvent,
} from "@/types/chat";
import { useLocale, useTranslations } from "next-intl";

let nextId = 1;

// Mirrors the platform-wide fallback business-backend resolves to when
// neither a conversation nor an organization has a temperature configured
// (see services/model_parameters.py::PLATFORM_DEFAULT_TEMPERATURE) — mock
// mode and a not-yet-created conversation have no backend record to read it
// from.
const DEFAULT_TEMPERATURE = 0.7;

function makeUserId(): string {
  return `u${nextId++}`;
}

function makeAssistantId(): string {
  return `a${nextId++}`;
}

function makeConversationId(): string {
  return `c${nextId++}`;
}

function getResponse(agent: Agent): AssistantMessage {
  if (agent.kind === "custom") return customResp(agent);
  switch (agent.id) {
    case "analyst":
      return analystResp();
    case "rag":
      return ragResp();
    case "sql":
      return sqlResp();
    case "swarm":
      return swarmResp();
    case "general":
    default:
      return generalResp();
  }
}

function makeInitialAssistantView(
  agentId: string,
  response: AssistantMessage
): AssistantMessageView {
  return {
    ...response,
    id: response.id || makeAssistantId(),
    role: "assistant",
    agentId,
    thinkingShown: "",
    contentShown: "",
    phase: "think",
    thinkingOpen: true,
    thinkingSeconds: 0,
    startedAt: Date.now(),
    tools: response.tools.map((tool) => ({ ...tool, state: "wait" })),
  };
}

function resetAssistantView(msg: AssistantMessageView): AssistantMessageView {
  return {
    ...msg,
    thinkingShown: "",
    contentShown: "",
    phase: "think",
    thinkingOpen: true,
    thinkingSeconds: 0,
    startedAt: Date.now(),
    sources: [],
    images: [],
    tools: msg.tools.map((tool) => ({ ...tool, state: "wait" })),
  };
}

function isRealMode(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.keycloak?.token &&
    !!getRuntimeConfig().apiUrl
  );
}

function backendMessageToUIMessage(
  msg: MessageResponse,
  agentId: string,
  sources: Source[] = []
): Message {
  if (msg.role === "user") {
    return {
      id: msg.id,
      role: "user",
      content: msg.content ?? "",
      images: imageIdsFrom(msg.imageIds),
      createdAt: msg.createdAt,
    };
  }
  return {
    id: msg.id,
    role: "assistant",
    agentId,
    thinking: "",
    thinkingShown: "",
    tools: toolCallsFromHistory(msg.toolCalls, msg.toolResults),
    content: msg.content ?? "",
    contentShown: msg.content ?? "",
    widgets: [],
    sources,
    images: imageIdsFrom(msg.imageIds),
    phase: "done",
    thinkingOpen: false,
    thinkingSeconds: 0,
    startedAt: new Date(msg.createdAt).getTime(),
    faithfulnessScore: msg.faithfulnessScore,
    faithfulnessReason: msg.faithfulnessReason,
    createdAt: msg.createdAt,
    failed: msg.failed === true,
  };
}

export interface UseChatReturn {
  messages: Message[];
  send: (
    text: string,
    workflowId?: string,
    imageIds?: string[],
    agent?: ChatAgentId
  ) => void;
  regenerate: (messageId: string) => void;
  isStreaming: boolean;
  isLoading: boolean;
  error: string | null;
  cancel: () => void;
  activeConversationId: string | null;
  questions: QuestionResponse[];
  hasActiveTurn: boolean;
  answerQuestion: (
    questionId: string,
    answer: QuestionAnswer,
    idempotencyKey: string
  ) => Promise<void>;
  /** Returns the active conversation's id, creating an empty backend
   * conversation first if none exists yet — used by the composer's file
   * attach button, which (unlike sending a message) has no text to derive
   * a conversation from. No-op (returns the existing id) once a
   * conversation is already active. Mock mode (no real backend) creates a
   * local-only id the same way sendMock does. */
  ensureConversationId: () => Promise<string>;
}

export function useChat(): UseChatReturn {
  const t = useTranslations("chat.runtime");
  const historyT = useTranslations("history");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const {
    agents,
    myAgents,
    activeAgentId,
    activeModelId,
    conversations,
    setConversations,
    activeConversationId,
    setActiveConversationId,
  } = useAppShell();

  const activeAgent = useMemo(
    () =>
      [...agents, ...myAgents].find((a) => a.id === activeAgentId) ??
      agents[0],
    [agents, myAgents, activeAgentId]
  );

  const activeConversation = useMemo(
    () => conversations.find((c) => c.id === activeConversationId) ?? null,
    [conversations, activeConversationId]
  );

  const messages = useMemo<Message[]>(
    () => activeConversation?.messages ?? [],
    [activeConversation]
  );
  const {
    questions,
    hasActiveTurn,
    answer: answerQuestion,
  } = useQuestions(activeConversationId);

  const generationRef = useRef(0);
  // Admission starts before ensureConversationId's await, not when the POST
  // stream is installed. Passive terminal hydration must respect that gap.
  const pendingSendRef = useRef<number | null>(null);
  const timeoutsRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancelRef = useRef<(() => void) | null>(null);
  const activeStreamRef = useRef<{
    conversationId: string;
    assistantId: string;
    workflowRunId?: string;
  } | null>(null);
  const observedTurnRef = useRef<{ conversationId: string | null; active: boolean } | null>(null);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  useEffect(() => {
    const previous = observedTurnRef.current;
    observedTurnRef.current = { conversationId: activeConversationId, active: hasActiveTurn };
    if (activeConversationId && previous?.conversationId === activeConversationId
      && previous.active && !hasActiveTurn && !activeStreamRef.current
      && pendingSendRef.current === null) {
      // A reopened chat has no original POST stream. Its durable turn SSE
      // invalidates the active-turn query on completion; hydrate the final and
      // owner-checked sources without requiring another history navigation.
      setHistoryRefresh((version) => version + 1);
    }
  }, [activeConversationId, hasActiveTurn]);
  const skipHistoryLoadRef = useRef<Set<string>>(new Set());
  // See the history-reload effect below — read there
  // instead of depending on activeAgentId directly, so switching agents
  // doesn't re-trigger that effect and clobber a live stream.
  const activeAgentIdRef = useRef(activeAgentId);
  useEffect(() => {
    activeAgentIdRef.current = activeAgentId;
  }, [activeAgentId]);
  const hydrationTextRef = useRef({ t, historyT, locale });
  useEffect(() => {
    hydrationTextRef.current = { t, historyT, locale };
  }, [t, historyT, locale]);

  const clearAllTimeouts = useCallback(() => {
    timeoutsRef.current.forEach(clearTimeout);
    timeoutsRef.current = [];
  }, []);

  const invalidatePendingSend = useCallback(() => {
    ++generationRef.current;
    pendingSendRef.current = null;
  }, []);

  const finalizeStreamingMessages = useCallback(
    (
      conversationId: string,
      cancelledByUser = false,
      targetAssistantId?: string
    ) => {
      setConversations((prev) =>
        prev.map((conv) => {
          if (conv.id !== conversationId) return conv;
          return {
            ...conv,
            messages: conv.messages.map((msg) => {
              if (
                msg.role !== "assistant" ||
                (targetAssistantId && msg.id !== targetAssistantId) ||
                (msg.phase === "done" && !cancelledByUser)
              ) {
                return msg;
              }
              const endedAt = Date.now();
              const workflow =
                cancelledByUser && msg.workflow
                  ? {
                      ...msg.workflow,
                      status: "cancelled" as const,
                      nodes: Object.fromEntries(
                        Object.entries(msg.workflow.nodes).map(([key, node]) => [
                          key,
                          node.status === "pending" || node.status === "running"
                            ? { ...node, status: "cancelled" as const }
                            : node,
                        ])
                      ),
                    }
                  : msg.workflow;
              return {
                ...msg,
                phase: "done" as const,
                thinkingOpen: false,
                thinkingSeconds: Math.max(
                  1,
                  Math.round((endedAt - msg.startedAt) / 1000)
                ),
                ...(workflow ? { workflow } : {}),
              };
            }),
          };
        })
      );
    },
    [setConversations]
  );

  const cancel = useCallback(() => {
    clearAllTimeouts();
    if (pendingSendRef.current !== null) {
      invalidatePendingSend();
    }
    const activeStream = activeStreamRef.current;
    if (activeStream?.workflowRunId && isRealMode()) {
      void cancelWorkflowRun(activeStream.workflowRunId)
        .then(() => {
          finalizeStreamingMessages(
            activeStream.conversationId,
            true,
            activeStream.assistantId
          );
          if (activeStreamRef.current?.assistantId === activeStream.assistantId) {
            cancelRef.current?.();
            cancelRef.current = null;
            activeStreamRef.current = null;
          }
          setIsLoading(false);
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : String(err));
        });
      return;
    }
    if (activeConversationId && isRealMode()) {
      void cancelChat(activeConversationId).catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
      });
    }
    if (activeConversationId) {
      finalizeStreamingMessages(activeConversationId, true);
    }
    cancelRef.current?.();
    cancelRef.current = null;
    activeStreamRef.current = null;
    setIsLoading(false);
  }, [clearAllTimeouts, activeConversationId, finalizeStreamingMessages, invalidatePendingSend]);

  const schedule = useCallback(
    (fn: () => void, delay: number) => {
      const id = setTimeout(fn, delay);
      timeoutsRef.current.push(id);
      return id;
    },
    []
  );

  const updateAssistantMessage = useCallback(
    (
      conversationId: string,
      messageId: string,
      updater: (msg: AssistantMessageView) => AssistantMessageView
    ) => {
      setConversations((prev) =>
        prev.map((conv) => {
          if (conv.id !== conversationId) return conv;
          return {
            ...conv,
            messages: conv.messages.map((msg) => {
              if (msg.role !== "assistant" || msg.id !== messageId) return msg;
              return updater(msg);
            }),
          };
        })
      );
    },
    [setConversations]
  );

  const refreshWebSourcesForTurn = useCallback(
    async (
      conversationId: string,
      assistantId: string,
      turnId: string,
      generation: number
    ) => {
      try {
        if (generationRef.current !== generation) return;
        const { messages: persistedMessages } =
          await getBackendConversation(conversationId);
        if (generationRef.current !== generation) return;
        const persisted = [...persistedMessages]
          .reverse()
          .find(
            (message) =>
              message.role === "assistant" && message.turnId === turnId
          );
        if (!persisted) return;
        const sources = await loadSourcesForMessage(conversationId, persisted);
        if (generationRef.current !== generation) return;
        // The persisted list is the backend-verified one: it replaces ids
        // collected from the live stream.
        const images = imageIdsFrom(persisted.imageIds);
        updateAssistantMessage(conversationId, assistantId, (message) =>
          generationRef.current === generation
            ? { ...message, sources, images }
            : message
        );
      } catch {
        // The answer remains usable when source metadata is temporarily
        // unavailable. Never construct clickable citations from SSE/model
        // output as a fallback; a later history reload retries the owner API.
      }
    },
    [updateAssistantMessage]
  );

  const typeText = useCallback(
    (
      text: string,
      onUpdate: (value: string) => void,
      onDone: () => void,
      delay = 12
    ) => {
      let i = 0;
      const step = () => {
        if (i >= text.length) {
          onDone();
          return;
        }
        i++;
        onUpdate(text.slice(0, i));
        schedule(step, delay);
      };
      schedule(step, delay);
    },
    [schedule]
  );

  const runAssistantPhases = useCallback(
    (
      conversationId: string,
      assistantId: string,
      response: AssistantMessage,
      gen: number
    ) => {
      const checkGen = () => generationRef.current === gen;

      const runTools = () => {
        if (!checkGen()) return;
        const tools = response.tools;
        if (tools.length === 0) {
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            phase: "answer",
          }));
          runAnswer();
          return;
        }

        let index = 0;
        const step = () => {
          if (!checkGen()) return;
          const currentIndex = index;
          if (currentIndex >= tools.length) {
            updateAssistantMessage(conversationId, assistantId, (msg) => ({
              ...msg,
              phase: "answer",
            }));
            runAnswer();
            return;
          }

          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            tools: msg.tools.map((tool, i) =>
              i === currentIndex ? { ...tool, state: "run" as const } : tool
            ),
          }));

          schedule(() => {
            if (!checkGen()) return;
            updateAssistantMessage(conversationId, assistantId, (msg) => ({
              ...msg,
              tools: msg.tools.map((tool, i) =>
                i === currentIndex ? { ...tool, state: "done" as const } : tool
              ),
            }));
            index++;
            schedule(step, 200);
          }, 350);
        };

        step();
      };

      const runAnswer = () => {
        if (!checkGen()) return;
        typeText(
          response.content,
          (value) => {
            if (!checkGen()) return;
            updateAssistantMessage(conversationId, assistantId, (msg) => ({
              ...msg,
              contentShown: value,
            }));
          },
          () => {
            if (!checkGen()) return;
            updateAssistantMessage(conversationId, assistantId, (msg) => {
              const endedAt = Date.now();
              return {
                ...msg,
                phase: "done",
                sources: response.sources,
                thinkingOpen: false,
                thinkingSeconds: Math.max(
                  1,
                  Math.round((endedAt - msg.startedAt) / 1000)
                ),
              };
            });
          },
          10
        );
      };

      typeText(
        response.thinking,
        (value) => {
          if (!checkGen()) return;
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            thinkingShown: value,
          }));
        },
        () => {
          if (!checkGen()) return;
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            phase: "tools",
          }));
          runTools();
        },
        10
      );
    },
    [schedule, typeText, updateAssistantMessage]
  );

  const handleSSEEvent = useCallback(
    (
      conversationId: string,
      assistantId: string,
      event: SSEEvent,
      generation: number
    ) => {
      switch (event.type) {
        case "message-start":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            phase: "tools",
          }));
          break;
        case "thinking-delta":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            phase: "think",
            thinking: msg.thinking + event.data.delta,
            thinkingShown: msg.thinkingShown + event.data.delta,
          }));
          break;
        case "content-delta":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            phase: "answer",
            contentShown: msg.contentShown + event.data.delta,
            content: msg.content + event.data.delta,
          }));
          break;
        case "tool-calls":
          updateAssistantMessage(conversationId, assistantId, (msg) => {
            // The runner publishes one `tool-calls` event per invocation, so
            // replacing the list erased the previous card and orphaned its
            // `tool-result`. Reloading the turn showed every call
            // (toolCallsFromHistory keeps them all), so the live stream and
            // the history disagreed — and web turns make search-then-read the
            // common case rather than the exception.
            const tools = [...msg.tools];
            for (const call of event.data.calls) {
              const incoming = {
                callId: call.call_id,
                ...(call.tool ? { toolId: call.tool } : {}),
                name: call.label ?? call.skill_id ?? call.tool ?? t("toolFallback"),
                detail: call.args ? JSON.stringify(call.args) : "",
                state: "run" as const,
              };
              const index = tools.findIndex(
                (tool) => tool.callId !== undefined && tool.callId === call.call_id
              );
              if (index === -1) {
                tools.push(incoming);
              } else {
                tools[index] = { ...incoming, state: tools[index].state };
              }
            }
            return { ...msg, phase: "tools", tools };
          });
          break;
        case "tool-result": {
          const result = event.data.result;
          const produced = imageIdsFrom(
            typeof result === "object" && result !== null
              ? (result as { imageIds?: unknown }).imageIds
              : undefined
          );
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            images: produced.length
              ? [...new Set([...(msg.images ?? []), ...produced])]
              : msg.images,
            tools: msg.tools.map((tool) =>
              tool.callId === event.data.call_id
                ? {
                    ...tool,
                    state: "done" as const,
                    detail: `${tool.detail}\n${JSON.stringify(event.data.result)}`,
                  }
                : tool
            ),
          }));
          break;
        }
        // Show each code-sandbox command and its result during a multi-round
        // session, so a long tools phase has visible execution progress
        // rather than appearing stalled.
        case "run-command":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            tools: msg.tools.map((tool) =>
              tool.name === event.data.skill_id && tool.state === "run"
                ? { ...tool, detail: `${tool.detail}\n$ ${event.data.command}` }
                : tool
            ),
          }));
          break;
        case "run-result": {
          const result = event.data.result ?? {};
          const summary = result.timed_out
            ? t("timedOut")
            : t("exitCode", { code: String(result.exit_code ?? "?") });
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            tools: msg.tools.map((tool) =>
              tool.name === event.data.skill_id && tool.state === "run"
                ? { ...tool, detail: `${tool.detail} ${summary}` }
                : tool
            ),
          }));
          break;
        }
        case "message-end":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            phase: "done",
            contentShown: event.data.content,
            content: event.data.content,
            failed: event.data.failed === true,
            thinkingOpen: false,
            thinkingSeconds: Math.max(
              1,
              Math.round((Date.now() - msg.startedAt) / 1000)
            ),
          }));
          setIsLoading(false);
          break;
        case "workflow-run-status":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            phase:
              event.data.status === "failed" ||
              event.data.status === "cancelled"
                ? "done"
                : msg.phase,
            workflow: {
              workflowRunId: event.data.workflowRunId,
              turnId: event.data.turnId,
              status: event.data.status,
              nodes:
                msg.workflow?.workflowRunId === event.data.workflowRunId
                  ? msg.workflow.nodes
                  : {},
              ...(event.data.errorCode
                ? { errorCode: event.data.errorCode }
                : {}),
            },
          }));
          break;
        case "workflow-node-status":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            workflow: {
              workflowRunId: event.data.workflowRunId,
              turnId: event.data.turnId,
              status:
                msg.workflow?.workflowRunId === event.data.workflowRunId
                  ? msg.workflow.status
                  : "running",
              nodes: {
                ...(msg.workflow?.workflowRunId === event.data.workflowRunId
                  ? msg.workflow.nodes
                  : {}),
                [event.data.nodeKey]: {
                  nodeKey: event.data.nodeKey,
                  nodeName: event.data.nodeName,
                  status: event.data.status,
                  ...(event.data.errorCode
                    ? { errorCode: event.data.errorCode }
                    : {}),
                },
              },
              ...(msg.workflow?.workflowRunId === event.data.workflowRunId &&
              msg.workflow.errorCode
                ? { errorCode: msg.workflow.errorCode }
                : {}),
            },
          }));
          break;
        case "question-status":
        case "turn-status":
          void queryClient.invalidateQueries({
            queryKey: questionKeys.list(conversationId),
          });
          void queryClient.invalidateQueries({
            queryKey: questionKeys.activeTurn(conversationId),
          });
          break;
        case "done":
          setIsLoading(false);
          finalizeStreamingMessages(conversationId);
          void refreshWebSourcesForTurn(
            conversationId,
            assistantId,
            event.data.turn_id,
            generation
          );
          queryClient.invalidateQueries({ queryKey: ["conversations"] });
          break;
        case "message-score":
          updateAssistantMessage(conversationId, assistantId, (msg) => ({
            ...msg,
            faithfulnessScore: event.data.score,
            faithfulnessReason: event.data.reason,
          }));
          break;
      }
    },
    [
      updateAssistantMessage,
      finalizeStreamingMessages,
      refreshWebSourcesForTurn,
      queryClient,
      t,
    ]
  );

  const sendMock = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      clearAllTimeouts();
      const gen = ++generationRef.current;

      let conversationId = activeConversationId;

      if (!conversationId) {
        conversationId = makeConversationId();
        const newConversation: Conversation = {
          id: conversationId,
          agentId: activeAgent.id,
          title: trimmed.slice(0, 60),
          date: historyT("buckets.today"),
          time: new Date().toLocaleTimeString(locale, {
            hour: "2-digit",
            minute: "2-digit",
          }),
          messages: [],
          model: activeModelId,
          temperature: DEFAULT_TEMPERATURE,
          lastMessageAt: new Date().toISOString(),
          status: "active",
          messageCount: 0,
        };
        setConversations((prev) => [newConversation, ...prev]);
        setActiveConversationId(conversationId);
      }

      const userMessage: UserMessage = {
        id: makeUserId(),
        role: "user",
        content: trimmed,
        createdAt: new Date().toISOString(),
      };

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === conversationId
            ? { ...conv, messages: [...conv.messages, userMessage] }
            : conv
        )
      );

      const response = getResponse(activeAgent);
      const assistantView = makeInitialAssistantView(activeAgent.id, response);
      const assistantId = assistantView.id;

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === conversationId
            ? { ...conv, messages: [...conv.messages, assistantView] }
            : conv
        )
      );

      runAssistantPhases(conversationId, assistantId, response, gen);
    },
    [
      activeAgent,
      activeConversationId,
      activeModelId,
      clearAllTimeouts,
      runAssistantPhases,
      setActiveConversationId,
      setConversations,
      historyT,
      locale,
    ]
  );

  const ensureConversationId = useCallback(
    async (titleHint = historyT("newConversation")): Promise<string> => {
      if (activeConversationId) return activeConversationId;

      if (!isRealMode()) {
        const conversationId = makeConversationId();
        const newConversation: Conversation = {
          id: conversationId,
          agentId: activeAgent.id,
          title: titleHint,
          date: historyT("buckets.today"),
          time: new Date().toLocaleTimeString(locale, {
            hour: "2-digit",
            minute: "2-digit",
          }),
          messages: [],
          model: activeModelId,
          temperature: DEFAULT_TEMPERATURE,
          lastMessageAt: new Date().toISOString(),
          status: "active",
          messageCount: 0,
        };
        setConversations((prev) => [newConversation, ...prev]);
        setActiveConversationId(conversationId);
        return conversationId;
      }

      const backendConv = await createBackendConversation({
        title: titleHint,
        model: activeModelId,
      });
      const conversationId = backendConv.id;
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      skipHistoryLoadRef.current.add(conversationId);
      const newConversation: Conversation = {
        id: conversationId,
        agentId: activeAgent.id,
        title: backendConv.title ?? titleHint,
        date: historyT("buckets.today"),
        time: new Date().toLocaleTimeString(locale, {
          hour: "2-digit",
          minute: "2-digit",
        }),
        messages: [],
        model: activeModelId,
        temperature: backendConv.temperature,
        lastMessageAt: new Date().toISOString(),
        status: "active",
        messageCount: 0,
      };
      setConversations((prev) => [newConversation, ...prev]);
      setActiveConversationId(conversationId);
      return conversationId;
    },
    [
      activeAgent,
      activeConversationId,
      activeModelId,
      queryClient,
      setActiveConversationId,
      setConversations,
      historyT,
      locale,
    ]
  );

  const sendBackend = useCallback(
    async (
      text: string,
      workflowId?: string,
      imageIds: string[] = [],
      agent?: ChatAgentId
    ) => {
      const trimmed = text.trim();
      if (!trimmed && imageIds.length === 0) return;

      clearAllTimeouts();
      // A previous turn might still be streaming (e.g. this slipped through
      // despite the Composer's own isStreaming guard) — cancel() tells the
      // backend to actually stop generating and finalizes the orphaned
      // assistant message, unlike a bare cancelRef abort, which only killed
      // our local fetch: the backend kept generating (burning tokens) and
      // the abandoned message stayed stuck mid-phase forever, which itself
      // kept isStreaming stuck true.
      if (messages.some((m) => m.role === "assistant" && m.phase !== "done")) {
        cancel();
      } else {
        cancelRef.current?.();
      }
      const gen = ++generationRef.current;

      setError(null);
      pendingSendRef.current = gen;
      setIsLoading(true);

      try {
        const conversationId = await ensureConversationId(trimmed.slice(0, 60));
        if (gen !== generationRef.current) return;
        const convId = conversationId;

        const userMessage: UserMessage = {
          id: makeUserId(),
          role: "user",
          content: trimmed,
          ...(imageIds.length > 0 ? { images: imageIds } : {}),
          createdAt: new Date().toISOString(),
        };

        setConversations((prev) =>
          prev.map((conv) =>
            conv.id === convId
              ? { ...conv, messages: [...conv.messages, userMessage] }
              : conv
          )
        );

        const assistantId = makeAssistantId();
        const assistantView: AssistantMessageView = {
          id: assistantId,
          role: "assistant",
          agentId: activeAgent.id,
          thinking: "",
          thinkingShown: "",
          content: "",
          contentShown: "",
          widgets: [],
          sources: [],
          tools: [],
          phase: "tools",
          thinkingOpen: false,
          thinkingSeconds: 0,
          startedAt: Date.now(),
          createdAt: new Date().toISOString(),
        };

        setConversations((prev) =>
          prev.map((conv) =>
            conv.id === convId
              ? { ...conv, messages: [...conv.messages, assistantView] }
              : conv
          )
        );

        // Named cancelFetch, not cancel — a `const cancel` here would shadow
        // the outer cancel() (from useChat's own scope) for this whole
        // function body, including the guard above that calls it.
        const { cancel: cancelFetch } = streamChat({
          conversation_id: convId,
          messages: [
            { role: "user", content: trimmed, ...(imageIds.length > 0 ? { imageIds } : {}) },
          ],
          model: activeModelId,
          options: { temperature: 0.7, max_tokens: 4096 },
          workflowId,
          agent,
          onOpen: () => {
            void queryClient.invalidateQueries({
              queryKey: questionKeys.list(convId),
            });
            void queryClient.invalidateQueries({
              queryKey: questionKeys.activeTurn(convId),
            });
          },
          onEvent: (event) => {
            if (gen !== generationRef.current) return;
            if (
              event.type === "workflow-run-status" ||
              event.type === "workflow-node-status"
            ) {
              const active = activeStreamRef.current;
              if (active?.assistantId === assistantId) {
                active.workflowRunId = event.data.workflowRunId;
              }
            }
            handleSSEEvent(convId, assistantId, event, gen);
            if (event.type === "done") {
              activeStreamRef.current = null;
              cancelRef.current = null;
            }
          },
          onError: (err) => {
            if (gen !== generationRef.current) return;
            setError(err.message);
            setIsLoading(false);
            // A stream that ends without message-end/done (see chat-api.ts)
            // also surfaces here — the assistant message must be finalized
            // to phase "done" or isStreaming (message-phase-based, not
            // isLoading-based) stays stuck true forever, blocking the
            // composer.
            finalizeStreamingMessages(convId);
            activeStreamRef.current = null;
            cancelRef.current = null;
          },
        });
        activeStreamRef.current = {
          conversationId: convId,
          assistantId,
        };
        cancelRef.current = cancelFetch;
      } catch (err) {
        if (gen !== generationRef.current) return;
        setError(err instanceof Error ? err.message : String(err));
        setIsLoading(false);
      } finally {
        if (pendingSendRef.current === gen) pendingSendRef.current = null;
      }
    },
    [
      activeAgent,
      activeModelId,
      cancel,
      clearAllTimeouts,
      ensureConversationId,
      finalizeStreamingMessages,
      handleSSEEvent,
      messages,
      queryClient,
      setConversations,
    ]
  );

  const send = useCallback(
    (
      text: string,
      workflowId?: string,
      imageIds?: string[],
      agent?: ChatAgentId
    ) => {
      if (isRealMode()) {
        void sendBackend(text, workflowId, imageIds, agent);
      } else {
        // Mock mode has no workflow runtime. Keep its existing deterministic
        // single-agent response even if a stale caller passes a selection.
        sendMock(text);
      }
    },
    [sendBackend, sendMock]
  );

  const regenerateMock = useCallback(
    (messageId: string) => {
      if (!activeConversationId || !activeConversation) return;

      const assistantMessage = activeConversation.messages.find(
        (m): m is AssistantMessageView =>
          m.role === "assistant" && m.id === messageId
      );
      if (!assistantMessage) return;

      clearAllTimeouts();
      const gen = ++generationRef.current;

      const response: AssistantMessage = {
        id: assistantMessage.id,
        role: "assistant",
        agentId: assistantMessage.agentId,
        thinking: assistantMessage.thinking,
        tools: assistantMessage.tools.map(({ state: _, ...rest }) => rest),
        content: assistantMessage.content,
        widgets: assistantMessage.widgets,
        sources: assistantMessage.sources,
        sql: assistantMessage.sql,
      };

      setConversations((prev) =>
        prev.map((conv) => {
          if (conv.id !== activeConversationId) return conv;
          return {
            ...conv,
            messages: conv.messages.map((msg) =>
              msg.role === "assistant" && msg.id === messageId
                ? resetAssistantView(msg)
                : msg
            ),
          };
        })
      );

      runAssistantPhases(activeConversationId, messageId, response, gen);
    },
    [
      activeConversation,
      activeConversationId,
      clearAllTimeouts,
      runAssistantPhases,
      setConversations,
    ]
  );

  const regenerateBackend = useCallback(
    async (messageId: string) => {
      if (!activeConversationId || !activeConversation) return;

      const userMessage = [...activeConversation.messages]
        .reverse()
        .find((m): m is UserMessage => m.role === "user");
      if (!userMessage) return;

      clearAllTimeouts();
      // Same guard as sendBackend — Regenerate clicked mid-stream must
      // properly cancel the in-flight turn on the backend, not just abort
      // our local fetch.
      if (messages.some((m) => m.role === "assistant" && m.phase !== "done")) {
        cancel();
      } else {
        cancelRef.current?.();
      }
      const gen = ++generationRef.current;

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === activeConversationId
            ? {
                ...conv,
                messages: conv.messages.map((msg) =>
                  msg.role === "assistant" && msg.id === messageId
                    ? resetAssistantView(msg)
                    : msg
                ),
              }
            : conv
        )
      );

      // Named cancelFetch, not cancel — a `const cancel` here would shadow
      // the outer cancel() for this whole function body, including the
      // guard above that calls it.
      const { cancel: cancelFetch } = streamChat({
        conversation_id: activeConversationId,
        messages: [{ role: "user", content: userMessage.content }],
        model: activeModelId,
        options: { temperature: 0.7, max_tokens: 4096 },
        onOpen: () => {
          void queryClient.invalidateQueries({
            queryKey: questionKeys.list(activeConversationId),
          });
          void queryClient.invalidateQueries({
            queryKey: questionKeys.activeTurn(activeConversationId),
          });
        },
        onEvent: (event) => {
          if (gen !== generationRef.current) return;
          handleSSEEvent(activeConversationId, messageId, event, gen);
        },
        onError: (err) => {
          if (gen !== generationRef.current) return;
          setError(err.message);
          setIsLoading(false);
          // See sendBackend's identical onError — a stream that ends
          // without message-end/done must still finalize the assistant
          // message's phase, or isStreaming stays stuck true (H12).
          finalizeStreamingMessages(activeConversationId);
        },
      });
      cancelRef.current = cancelFetch;
    },
    [
      activeConversation,
      activeConversationId,
      activeModelId,
      cancel,
      clearAllTimeouts,
      finalizeStreamingMessages,
      handleSSEEvent,
      messages,
      queryClient,
      setConversations,
    ]
  );

  const regenerate = useCallback(
    (messageId: string) => {
      if (isRealMode()) {
        void regenerateBackend(messageId);
      } else {
        regenerateMock(messageId);
      }
    },
    [regenerateBackend, regenerateMock]
  );

  useEffect(() => {
    // Read through a ref: translator identity changes on server refreshes, and
    // re-running this effect would re-fetch history and drop live workflow cards.
    const text = hydrationTextRef.current;
    if (!activeConversationId || !isRealMode()) return;
    if (skipHistoryLoadRef.current.has(activeConversationId)) {
      skipHistoryLoadRef.current.delete(activeConversationId);
      return;
    }
    if (pendingSendRef.current !== null
      || activeStreamRef.current?.conversationId === activeConversationId) return;
    let cancelled = false;
    const hydrationGeneration = generationRef.current;
    setIsLoading(true);
    setError(null);

    getBackendConversation(activeConversationId)
      .then(async ({ conversation, messages }) => ({
        conversation,
        messages,
        hydratedSources: await Promise.all(
          messages.map((message) =>
            loadSourcesForMessage(activeConversationId, message)
          )
        ),
      }))
      .then(({ conversation, messages, hydratedSources }) => {
        if (cancelled || hydrationGeneration !== generationRef.current) return;
        setConversations((prev) => {
          if (cancelled || hydrationGeneration !== generationRef.current) return prev;
          const existing = prev.find((c) => c.id === conversation.id);
          const agentId = existing?.agentId ?? activeAgentIdRef.current;
          // History does not persist workflow progress, and live placeholders
          // carry local ids. Re-attach a card to the final of the same turn.
          const liveWorkflows = new Map(
            (existing?.messages ?? []).flatMap((m) =>
              m.role === "assistant" && m.workflow?.turnId
                ? [[m.workflow.turnId, m.workflow] as const]
                : []
            )
          );
          const restoredTurns = new Set<string>();
          const hydratedMessages = messages.map((message, index) => {
            const hydrated = backendMessageToUIMessage(
              message,
              agentId,
              hydratedSources[index]
            );
            const workflow = message.turnId ? liveWorkflows.get(message.turnId) : undefined;
            if (hydrated.role !== "assistant" || !workflow || !message.turnId) return hydrated;
            restoredTurns.add(message.turnId);
            return { ...hydrated, workflow };
          });
          // A workflow-run turn that failed or was cancelled before producing
          // any assistant content is never persisted (conversation.py returns
          // before persist_assistant_message once result.status !== "completed"),
          // and backendMessageToUIMessage never restores a `workflow` field
          // regardless — so a plain replace here erases the terminal decision
          // (e.g. a rejection) the live SSE stream had *just* applied the
          // instant this hydration lands, leaving the progress panel frozen on
          // its last pre-terminal state forever. Carry such local-only
          // messages forward instead of dropping them.
          const hydratedIds = new Set(hydratedMessages.map((m) => m.id));
          const survivingWorkflowMessages = (existing?.messages ?? []).filter(
            (m): m is AssistantMessageView =>
              m.role === "assistant" &&
              !hydratedIds.has(m.id) &&
              !(m.workflow?.turnId && restoredTurns.has(m.workflow.turnId)) &&
              (m.workflow?.status === "failed" || m.workflow?.status === "cancelled")
          );
          const updated: Conversation = {
            ...(existing ?? {
              id: conversation.id,
              agentId,
              title: conversation.title ?? text.t("conversationFallback"),
              date: text.historyT("buckets.today"),
              time: new Date().toLocaleTimeString(text.locale, {
                hour: "2-digit",
                minute: "2-digit",
              }),
              model: conversation.model,
              temperature: conversation.temperature,
              lastMessageAt: conversation.lastMessageAt ?? conversation.createdAt,
              status: conversation.status,
              messageCount: 0,
            }),
            title: conversation.title ?? existing?.title ?? text.t("conversationFallback"),
            messages: [...hydratedMessages, ...survivingWorkflowMessages],
            temperature: conversation.temperature,
            lastMessageAt: conversation.lastMessageAt ?? conversation.createdAt,
            messageCount: messages.length + survivingWorkflowMessages.length,
            // The composer keeps the conversation's agent chosen after a reload.
            agent: conversation.agent ?? null,
          };
          if (existing) {
            return prev.map((c) => (c.id === conversation.id ? updated : c));
          }
          return [updated, ...prev];
        });
      })
      .catch((err) => {
        if (cancelled || hydrationGeneration !== generationRef.current) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled && hydrationGeneration === generationRef.current) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // activeAgentId deliberately excluded — clicking a
    // different agent while a conversation is open used to re-run this
    // effect, refetching the persisted (pre-stream) message list and
    // clobbering a live SSE stream in progress. activeAgentId is still read
    // above via activeAgentIdRef (kept in sync below) for the "brand new
    // conversation" fallback, just not as a re-run trigger.
  }, [activeConversationId, setConversations, historyRefresh]);

  useEffect(() => {
    return () => {
      invalidatePendingSend();
      clearAllTimeouts();
      cancelRef.current?.();
      cancelRef.current = null;
      activeStreamRef.current = null;
    };
  }, [clearAllTimeouts, invalidatePendingSend]);

  const isStreaming = useMemo(
    () => messages.some((m) => m.role === "assistant" && m.phase !== "done"),
    [messages]
  );

  return {
    messages,
    send,
    regenerate,
    isStreaming,
    isLoading,
    error,
    cancel,
    activeConversationId,
    questions,
    hasActiveTurn,
    answerQuestion,
    ensureConversationId,
  };
}
