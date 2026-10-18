import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { ModelSettingsDialog } from "@/components/aurum/chat/ModelSettingsDialog";
import * as chatApi from "@/lib/chat-api";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));

function Wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <AppShellProvider>{children}</AppShellProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("renders nothing when closed", () => {
  render(
    <ModelSettingsDialog
      open={false}
      conversationId="conv-1"
      temperature={0.7}
      onClose={vi.fn()}
    />,
    { wrapper: Wrapper }
  );

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("shows the current temperature and saves an override", async () => {
  vi.spyOn(chatApi, "updateConversation").mockResolvedValue({
    id: "conv-1",
    title: "Conversation",
    model: "gpt-4o-mini",
    temperature: 1.2,
    status: "active",
    lastMessageAt: null,
    createdAt: "2026-09-12T00:00:00Z",
    messageCount: 0,
    tokenCount: 0,
    costUsd: 0,
  });
  const onClose = vi.fn();

  render(
    <ModelSettingsDialog
      open
      conversationId="conv-1"
      temperature={0.7}
      onClose={onClose}
    />,
    { wrapper: Wrapper }
  );

  expect(screen.getByRole("dialog", { name: "Model settings" })).toBeInTheDocument();
  expect(screen.getByText("0.7")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Temperature"), {
    target: { value: "1.2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => {
    expect(chatApi.updateConversation).toHaveBeenCalledWith("conv-1", {
      temperature: 1.2,
    });
  });
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
});

test("calls onClose without saving when cancelled", () => {
  const updateSpy = vi.spyOn(chatApi, "updateConversation");
  const onClose = vi.fn();
  render(
    <ModelSettingsDialog
      open
      conversationId="conv-1"
      temperature={0.7}
      onClose={onClose}
    />,
    { wrapper: Wrapper }
  );

  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(updateSpy).not.toHaveBeenCalled();
});

test("calls onClose on Escape", () => {
  const onClose = vi.fn();
  render(
    <ModelSettingsDialog
      open
      conversationId="conv-1"
      temperature={0.7}
      onClose={onClose}
    />,
    { wrapper: Wrapper }
  );

  fireEvent.keyDown(document, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
});
