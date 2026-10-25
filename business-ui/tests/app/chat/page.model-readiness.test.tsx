import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import ChatPage from "@/app/chat/page";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { createTranslator } from "next-intl";
import { localizeAgents } from "@/lib/mock-data";
import en from "../../../messages/en.json";

const agents = localizeAgents(createTranslator({ locale: "en", messages: en, namespace: "agents" }));

let modelsReady = false;
const send = vi.fn();
vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: modelsReady ? { data: [{ id: "fixture-model" }] } : undefined }),
}));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ isPending: false }) }));
vi.mock("@/hooks/useWorkflows", () => ({ useWorkflows: () => ({ data: [], isLoading: false }) }));
vi.mock("@/hooks/useDocuments", () => ({
  useAttachFile: () => ({ mutateAsync: vi.fn() }),
  useDocuments: () => ({ data: [] }),
}));
vi.mock("@/hooks/useChat", () => ({
  useChat: () => ({
    messages: [], send, regenerate: vi.fn(), isStreaming: false,
    cancel: vi.fn(), error: null, activeConversationId: null, questions: [],
    hasActiveTurn: false, answerQuestion: vi.fn(), ensureConversationId: vi.fn(),
  }),
}));

beforeEach(() => { modelsReady = false; send.mockClear(); });

function Page() { return <AppShellProvider><ChatPage /></AppShellProvider>; }

test("authenticated users cannot send an empty model while the catalog loads", () => {
  const view = render(<Page />);
  expect(screen.getByLabelText("Message")).toBeDisabled();
  expect(screen.getByLabelText("Attach file")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Choose model" })).not.toBeDisabled();
  modelsReady = true;
  view.rerender(<Page />);
  const input = screen.getByLabelText("Message");
  expect(input).not.toBeDisabled();
  fireEvent.change(input, { target: { value: "First message" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(send).toHaveBeenCalledWith("First message", undefined, undefined);
});

test("suggestions wait for a selected model and become usable when it arrives", () => {
  const view = render(<Page />);
  const prompt = agents[0].suggestions[0];
  fireEvent.click(screen.getByRole("button", { name: prompt }));
  expect(send).not.toHaveBeenCalled();
  modelsReady = true;
  view.rerender(<Page />);
  fireEvent.click(screen.getByRole("button", { name: prompt }));
  expect(send).toHaveBeenCalledOnce();
  expect(send).toHaveBeenCalledWith(prompt, undefined, undefined);
});
