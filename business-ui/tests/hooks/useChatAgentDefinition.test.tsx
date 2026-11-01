import { render, renderHook, act, waitFor, screen } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { useChat } from "@/hooks/useChat";
import { useAppShell } from "@/hooks/useAppShell";
import { ModelPickerMenu } from "@/components/aurum/chat/ModelPickerMenu";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

vi.mock("@/lib/chat-api", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  streamChat: vi.fn(),
  cancelChat: vi.fn(),
  toolCallsFromHistory: vi.fn(() => []),
}));
vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true, token: "token" }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));
vi.mock("@/hooks/useChatAgentDefinitions", () => ({
  useChatAgentDefinitions: () => ({
    data: [
      { id: "def-1", name: "Procedures assistant", description: "Answers from procedures", modelId: "agent-model" },
    ],
  }),
}));

const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
const originalKeycloak = window.keycloak;

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000/v1";
  window.keycloak = { token: "token", updateToken: vi.fn().mockResolvedValue(true) };
});

afterEach(() => {
  process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  window.keycloak = originalKeycloak;
  vi.clearAllMocks();
});

function wrapper(activeAgentId: string) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(AppShellProvider, { initialState: { activeAgentId } }, children)
    );
  };
}

async function mockConversation() {
  const { createConversation, getConversation, streamChat } = await import("@/lib/chat-api");
  const conversation = {
    id: "conv-1", title: "New", model: "pro", temperature: 0.7, status: "active",
    lastMessageAt: null, createdAt: "2026-06-19T10:00:00Z", messageCount: 0, tokenCount: 0, costUsd: 0,
  };
  vi.mocked(createConversation).mockResolvedValue(conversation);
  vi.mocked(getConversation).mockResolvedValue({ conversation, messages: [] });
  vi.mocked(streamChat).mockReturnValue({ cancel: vi.fn() });
  return streamChat;
}

test("signed in, the shell offers the ordinary chat and the administrator's agents, not the demo catalog", () => {
  function Agents() {
    const { agents } = useAppShell();
    return <span data-testid="names">{agents.map((agent) => agent.name).join("|")}</span>;
  }
  render(React.createElement(wrapper("general"), null, <Agents />));

  expect(screen.getByTestId("names").textContent).toBe("General assistant|Procedures assistant");
});

test("a turn on an administrator's agent names it in the request", async () => {
  const streamChat = await mockConversation();
  const { result } = renderHook(() => useChat(), { wrapper: wrapper("def-1") });

  act(() => result.current.send("what is the allowed pH"));

  await waitFor(() => expect(streamChat).toHaveBeenCalled());
  expect(vi.mocked(streamChat).mock.calls[0][0].agentDefinitionId).toBe("def-1");
});

test("a workflow chosen for the turn replaces the agent in the request", async () => {
  const streamChat = await mockConversation();
  const { result } = renderHook(() => useChat(), { wrapper: wrapper("def-1") });

  act(() => result.current.send("run it", "workflow-1"));

  await waitFor(() => expect(streamChat).toHaveBeenCalled());
  const options = vi.mocked(streamChat).mock.calls[0][0];
  expect(options.workflowId).toBe("workflow-1");
  expect(options.agentDefinitionId).toBeUndefined();
});

test("the ordinary chat sends no agent", async () => {
  const streamChat = await mockConversation();
  const { result } = renderHook(() => useChat(), { wrapper: wrapper("general") });

  act(() => result.current.send("hello"));

  await waitFor(() => expect(streamChat).toHaveBeenCalled());
  expect(vi.mocked(streamChat).mock.calls[0][0].agentDefinitionId).toBeUndefined();
});

test("an agent that is no longer offered falls back to the ordinary chat", async () => {
  const streamChat = await mockConversation();
  const { result } = renderHook(() => useChat(), { wrapper: wrapper("archived-agent") });

  act(() => result.current.send("hello"));

  await waitFor(() => expect(streamChat).toHaveBeenCalled());
  expect(vi.mocked(streamChat).mock.calls[0][0].agentDefinitionId).toBeUndefined();
});

test("the model picker shows the agent's model instead of offering a choice", () => {
  render(React.createElement(wrapper("def-1"), null, <ModelPickerMenu />));

  expect(screen.getByText("agent-model")).toBeInTheDocument();
  expect(screen.getByLabelText("The model is set by the agent: agent-model")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Choose a model" })).not.toBeInTheDocument();
});
