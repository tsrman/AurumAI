import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, expect, test, vi } from "vitest";
import { useQuestions } from "../useQuestions";
import * as questionsApi from "@/lib/questions-api";
import type { QuestionResponse, TurnStreamEvent } from "@/types/chat";

vi.mock("@/lib/questions-api", () => ({
  listQuestions: vi.fn(),
  getActiveTurn: vi.fn(),
  answerQuestion: vi.fn(),
  subscribeTurnEvents: vi.fn(),
}));

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

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(questionsApi.listQuestions).mockResolvedValue([question]);
  vi.mocked(questionsApi.getActiveTurn).mockResolvedValue({
    turnId: "turn-1",
    status: "active",
    version: 1,
    questions: [{ questionId: "question-1", status: "pending", version: 1 }],
  });
  vi.mocked(questionsApi.answerQuestion).mockResolvedValue({
    ...question,
    status: "answered",
    version: 2,
    answer: { text: "Berlin" },
    answeredAt: "2026-09-06T10:05:00Z",
  });
});

test("two independent tabs refetch once for each newer entity version", async () => {
  const callbacks: Array<(event: TurnStreamEvent) => void> = [];
  vi.mocked(questionsApi.subscribeTurnEvents).mockImplementation(
    (_conversationId, _turnId, onEvent) => {
      callbacks.push(onEvent);
      return { cancel: vi.fn() };
    }
  );
  const firstClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const secondClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const first = renderHook(() => useQuestions("conversation-1"), {
    wrapper: wrapper(firstClient),
  });
  const second = renderHook(() => useQuestions("conversation-1"), {
    wrapper: wrapper(secondClient),
  });

  await waitFor(() => expect(callbacks).toHaveLength(2));
  await waitFor(() => expect(questionsApi.listQuestions).toHaveBeenCalledTimes(2));

  const event: TurnStreamEvent = {
    type: "question-status",
    data: {
      questionId: "question-1",
      turnId: "turn-1",
      status: "answered",
      version: 2,
    },
  };
  act(() => callbacks.forEach((callback) => callback(event)));

  await waitFor(() => expect(questionsApi.listQuestions).toHaveBeenCalledTimes(4));
  act(() => callbacks.forEach((callback) => callback(event)));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(questionsApi.listQuestions).toHaveBeenCalledTimes(4);

  const turnEvent: TurnStreamEvent = {
    type: "turn-status",
    data: {
      turnId: "turn-1",
      status: "completed",
      version: 2,
    },
  };
  act(() => callbacks.forEach((callback) => callback(turnEvent)));
  await waitFor(() => expect(questionsApi.getActiveTurn).toHaveBeenCalledTimes(4));
  act(() => callbacks.forEach((callback) => callback(turnEvent)));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(questionsApi.getActiveTurn).toHaveBeenCalledTimes(4);

  first.unmount();
  second.unmount();
});

test("answers through the API and exposes the refreshed question", async () => {
  vi.mocked(questionsApi.subscribeTurnEvents).mockReturnValue({ cancel: vi.fn() });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useQuestions("conversation-1"), {
    wrapper: wrapper(client),
  });
  await waitFor(() => expect(result.current.questions).toEqual([question]));

  await act(async () => {
    await result.current.answer(
      "question-1",
      { text: "Berlin" },
      "11111111-1111-4111-8111-111111111111"
    );
  });

  expect(questionsApi.answerQuestion).toHaveBeenCalledWith(
    "conversation-1",
    "question-1",
    { text: "Berlin" },
    "11111111-1111-4111-8111-111111111111"
  );
  await waitFor(() =>
    expect(result.current.questions[0]).toMatchObject({ status: "answered", version: 2 })
  );
});
