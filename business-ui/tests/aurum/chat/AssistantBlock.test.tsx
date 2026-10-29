import { render, screen, fireEvent } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import { AssistantBlock } from "@/components/aurum/chat/AssistantBlock";
import { AppShellProvider } from "@/providers/AppShellProvider";
import type { AssistantMessageView } from "@/types/aurum";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: false, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));

function Wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(AppShellProvider, null, children);
}

const baseMessage: AssistantMessageView = {
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

test("renders assistant message with accessible article", () => {
  render(React.createElement(AssistantBlock, { message: baseMessage }), {
    wrapper: Wrapper,
  });

  expect(screen.getByTestId("assistant-message")).toBeInTheDocument();
  expect(screen.getByText("Hello! I am ready to help.")).toBeInTheDocument();
  expect(screen.getByText("General assistant")).toBeInTheDocument();
});

test("renders workflow progress without raw orchestration fields", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    content: "",
    contentShown: "",
    workflow: {
      workflowRunId: "run-1",
      status: "synthesizing",
      nodes: {
        research: {
          nodeKey: "research",
          nodeName: "Research",
          status: "completed",
        },
      },
    },
  };

  render(React.createElement(AssistantBlock, { message }), { wrapper: Wrapper });

  expect(screen.getByText("Composing the answer")).toBeInTheDocument();
  expect(screen.getByText("Research")).toBeInTheDocument();
  expect(screen.queryByText("run-1")).not.toBeInTheDocument();
});

test("expands and collapses thinking box", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    thinkingOpen: true,
  };

  render(React.createElement(AssistantBlock, { message }), {
    wrapper: Wrapper,
  });

  const toggle = screen.getByRole("button", { name: /Reasoning/ });
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("Thinking...")).toBeInTheDocument();

  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
});

test("renders tool calls with accessible state labels", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    tools: [
      { name: "Database query", detail: "production", state: "done" },
      { name: "Aggregation", detail: "by hour", state: "run" },
    ],
  };

  render(React.createElement(AssistantBlock, { message }), {
    wrapper: Wrapper,
  });

  expect(screen.getByLabelText("Database query: Done")).toBeInTheDocument();
  expect(screen.getByLabelText("Aggregation: Running")).toBeInTheDocument();
});

test("renders metrics widget", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    widgets: [
      {
        type: "metrics",
        items: [
          { label: "Extraction", value: "86 %", delta: "+2 %", isPositive: true },
        ],
      },
    ],
  };

  render(React.createElement(AssistantBlock, { message }), {
    wrapper: Wrapper,
  });

  expect(screen.getByText("Extraction")).toBeInTheDocument();
  expect(screen.getByText("86 %")).toBeInTheDocument();
});

test("renders bars widget", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    widgets: [
      {
        type: "bars",
        title: "Extraction by hour",
        unit: " %",
        max: 90,
        data: [{ label: "08:00", value: 83 }],
      },
    ],
  };

  render(React.createElement(AssistantBlock, { message }), {
    wrapper: Wrapper,
  });

  expect(screen.getByRole("img", { name: "Extraction by hour" })).toBeInTheDocument();
});

test("renders table widget", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    widgets: [
      {
        type: "table",
        columns: ["Shift", "Value"],
        rows: [["A", "100"]],
      },
    ],
  };

  render(React.createElement(AssistantBlock, { message }), {
    wrapper: Wrapper,
  });

  expect(screen.getByRole("table", { name: "Query result" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "Shift" })).toBeInTheDocument();
});

test("renders SQL block", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    sql: "SELECT * FROM table",
  };

  render(React.createElement(AssistantBlock, { message }), {
    wrapper: Wrapper,
  });

  expect(screen.getByText("SELECT * FROM table")).toBeInTheDocument();
  expect(screen.getByLabelText("SQL query")).toBeInTheDocument();
});

test("shows pending indicator before any thinking, tools, or content arrive", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    thinking: "",
    thinkingShown: "",
    tools: [],
    content: "",
    contentShown: "",
    phase: "tools",
  };

  render(React.createElement(AssistantBlock, { message }), { wrapper: Wrapper });

  expect(screen.getByTestId("pending-pulse")).toBeInTheDocument();
});

test("hides pending indicator once tool calls appear", () => {
  const message: AssistantMessageView = {
    ...baseMessage,
    thinking: "",
    thinkingShown: "",
    tools: [{ name: "Database query", detail: "production", state: "run" }],
    content: "",
    contentShown: "",
    phase: "tools",
  };

  render(React.createElement(AssistantBlock, { message }), { wrapper: Wrapper });

  expect(screen.queryByTestId("pending-pulse")).not.toBeInTheDocument();
});

test("does not show pending indicator once real content has streamed", () => {
  render(React.createElement(AssistantBlock, { message: baseMessage }), {
    wrapper: Wrapper,
  });

  expect(screen.queryByTestId("pending-pulse")).not.toBeInTheDocument();
});

test("calls onRegenerate when regenerate button clicked", () => {
  const onRegenerate = vi.fn();

  render(
    React.createElement(AssistantBlock, {
      message: baseMessage,
      onRegenerate,
    }),
    { wrapper: Wrapper }
  );

  fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
  expect(onRegenerate).toHaveBeenCalledWith("a1");
});

test("a failed turn's reason is set apart as plain text and offers to try again", () => {
  const onRegenerate = vi.fn();
  const message: AssistantMessageView = {
    ...baseMessage,
    thinking: "",
    thinkingShown: "",
    content: "Could not get a model response. Reason: **quota** (hermes_model_unavailable)",
    contentShown: "Could not get a model response. Reason: **quota** (hermes_model_unavailable)",
    failed: true,
  };
  render(React.createElement(AssistantBlock, { message, onRegenerate }), { wrapper: Wrapper });

  expect(screen.getByText("The answer could not be completed")).toBeInTheDocument();
  // Not rendered as Markdown: the reason is shown as it is.
  expect(screen.getByTestId("assistant-content")).toHaveTextContent("Reason: **quota**");
  expect(screen.queryByRole("button", { name: "Copy answer" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Regenerate" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(onRegenerate).toHaveBeenCalledWith("a1");
});

test("an ordinary answer keeps its copy and regenerate actions", () => {
  render(React.createElement(AssistantBlock, { message: baseMessage }), { wrapper: Wrapper });

  expect(screen.getByRole("button", { name: "Copy answer" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Regenerate" })).toBeInTheDocument();
  expect(screen.queryByText("The answer could not be completed")).not.toBeInTheDocument();
});
