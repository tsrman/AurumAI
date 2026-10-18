import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { useAppShell } from "@/hooks/useAppShell";
import { ChatHeader } from "@/components/aurum/chat/ChatHeader";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: false, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));

function Wrapper({ children }: { children: React.ReactNode }) {
  return <AppShellProvider>{children}</AppShellProvider>;
}

function ActiveConversationSpy() {
  const { activeConversationId, setActiveConversationId } = useAppShell();
  return (
    <div>
      <button onClick={() => setActiveConversationId("conv-1")}>
        Set conversation
      </button>
      <span data-testid="active-conversation">
        {activeConversationId ?? "null"}
      </span>
    </div>
  );
}

test("renders active agent name and kind label", () => {
  render(<ChatHeader />, { wrapper: Wrapper });
  expect(screen.getByText("General assistant")).toBeInTheDocument();
  expect(screen.getByText("General-purpose helper")).toBeInTheDocument();
});

test("renders the New chat button", () => {
  render(<ChatHeader />, { wrapper: Wrapper });
  expect(screen.getByRole("button", { name: /New chat/i })).toBeInTheDocument();
});

test("New chat resets active conversation", () => {
  render(
    <>
      <ChatHeader />
      <ActiveConversationSpy />
    </>,
    { wrapper: Wrapper }
  );

  fireEvent.click(screen.getByRole("button", { name: /Set conversation/i }));
  expect(screen.getByTestId("active-conversation")).toHaveTextContent("conv-1");

  fireEvent.click(screen.getByRole("button", { name: /New chat/i }));
  expect(screen.getByTestId("active-conversation")).toHaveTextContent("null");
});
