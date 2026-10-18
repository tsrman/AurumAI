import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import { MessageList } from "@/components/aurum/chat/MessageList";
import { AppShellProvider } from "@/providers/AppShellProvider";
import type { AssistantMessageView, UserMessage } from "@/types/aurum";
import type { QuestionResponse } from "@/types/chat";

window.HTMLElement.prototype.scrollIntoView = vi.fn();

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: false, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));

function Wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(AppShellProvider, null, children);
}

const userMessage: UserMessage = {
  id: "u1",
  role: "user",
  content: "Hello, agent",
};

const assistantMessage: AssistantMessageView = {
  id: "a1",
  role: "assistant",
  agentId: "general",
  thinking: "Thinking...",
  thinkingShown: "Thinking...",
  tools: [],
  content: "Hello! I am ready to help.",
  contentShown: "Hello! I am ready to help.",
  widgets: [],
  sources: [],
  phase: "done",
  thinkingOpen: false,
  thinkingSeconds: 1,
  startedAt: Date.now(),
};

test("renders user and assistant messages", () => {
  render(React.createElement(MessageList, { messages: [userMessage, assistantMessage] }), {
    wrapper: Wrapper,
  });

  expect(screen.getByText("Hello, agent")).toBeInTheDocument();
  expect(screen.getByText("Hello! I am ready to help.")).toBeInTheDocument();
  expect(screen.getByText("General assistant")).toBeInTheDocument();
});

test("renders empty list", () => {
  const { container } = render(
    React.createElement(MessageList, { messages: [] }),
    { wrapper: Wrapper }
  );
  expect(container.querySelector("[data-testid='assistant-message']")).toBeNull();
});

test("calls onRegenerate when regenerate is triggered", () => {
  const onRegenerate = vi.fn();
  render(
    React.createElement(MessageList, {
      messages: [userMessage, assistantMessage],
      onRegenerate,
    }),
    { wrapper: Wrapper }
  );

  fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
  expect(onRegenerate).toHaveBeenCalledWith("a1");
});

test("merges questions into the message timeline by timestamp and forwards answers", async () => {
  const onAnswer = vi.fn().mockResolvedValue(undefined);
  const question: QuestionResponse = {
    questionId: "q1",
    turnId: "t1",
    text: "Specify the city",
    options: [{ id: "kazan", label: "Berlin" }],
    status: "pending",
    version: 1,
    expiresAt: "2099-01-01T00:00:00Z",
    createdAt: "2026-09-06T10:01:00Z",
    phase: "ordinary",
    nodeKey: null,
    agentName: "Analyst",
    answer: null,
    answeredAt: null,
  };
  const timedUser = { ...userMessage, createdAt: "2026-09-06T10:00:00Z" };
  const timedAssistant = {
    ...assistantMessage,
    createdAt: "2026-09-06T10:02:00Z",
  };
  const { container } = render(
    <MessageList
      messages={[timedAssistant, timedUser]}
      questions={[question]}
      onAnswer={onAnswer}
    />,
    { wrapper: Wrapper }
  );

  const content = container.textContent ?? "";
  expect(content.indexOf("Hello, agent")).toBeLessThan(content.indexOf("Specify the city"));
  expect(content.indexOf("Specify the city")).toBeLessThan(
    content.indexOf("Hello! I am ready to help.")
  );

  fireEvent.click(screen.getByRole("button", { name: "Berlin" }));
  await waitFor(() =>
    expect(onAnswer).toHaveBeenCalledWith(
      "q1",
      { optionId: "kazan" },
      expect.any(String)
    )
  );
});

test("scrolls the timeline when a question arrives without a new message", () => {
  const scrollIntoView = vi.mocked(window.HTMLElement.prototype.scrollIntoView);
  scrollIntoView.mockClear();
  const messages = [userMessage];
  const { rerender } = render(
    <MessageList messages={messages} questions={[]} />,
    { wrapper: Wrapper }
  );
  expect(scrollIntoView).toHaveBeenCalledTimes(1);

  rerender(
    <MessageList
      messages={messages}
      questions={[
        {
          questionId: "q2",
          turnId: "t1",
          text: "New question",
          options: [],
          status: "pending",
          version: 1,
          expiresAt: "2099-01-01T00:00:00Z",
          createdAt: "2026-09-06T10:01:00Z",
          phase: "ordinary",
          nodeKey: null,
          agentName: null,
          answer: null,
          answeredAt: null,
        },
      ]}
    />
  );

  expect(scrollIntoView).toHaveBeenCalledTimes(2);
});
