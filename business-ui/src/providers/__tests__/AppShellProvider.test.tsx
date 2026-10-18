import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppShell } from "@/hooks/useAppShell";
import { useModels } from "@/hooks/useModels";
import { useSettings } from "@/hooks/useSettings";
import { useAuth } from "@/providers/AuthProvider";
import { AppShellProvider } from "../AppShellProvider";

vi.mock("@/providers/AuthProvider");
vi.mock("@/hooks/useModels");
vi.mock("@/hooks/useSettings");

const STORAGE_KEY =
  "aurum-ai.active-conversation:550e8400-e29b-41d4-a716-446655440001:user-1";

function ActiveConversationProbe() {
  const { activeConversationId, setActiveConversationId } = useAppShell();
  return (
    <>
      <output>{activeConversationId ?? "none"}</output>
      <button onClick={() => setActiveConversationId("conversation-2")}>open</button>
      <button onClick={() => setActiveConversationId(null)}>clear</button>
    </>
  );
}

describe("AppShellProvider active conversation recovery", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.sessionStorage.clear();
    vi.mocked(useAuth).mockReturnValue({
      user: {
        userId: "user-1",
        orgId: "550e8400-e29b-41d4-a716-446655440001",
        role: "member",
        token: "token",
      },
      token: "token",
      isAuthenticated: true,
      isLoading: false,
      capabilities: [],
      authorizationReady: true,
      hasCapability: vi.fn(() => false),
      login: vi.fn(),
      logout: vi.fn(),
    });
    vi.mocked(useModels).mockReturnValue({
      data: { data: [] },
    } as unknown as ReturnType<typeof useModels>);
    vi.mocked(useSettings).mockReturnValue({
      data: undefined,
      isPending: false,
    } as ReturnType<typeof useSettings>);
  });

  it("restores the identity-scoped conversation after a reload", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, "conversation-1");

    render(
      <AppShellProvider>
        <ActiveConversationProbe />
      </AppShellProvider>
    );

    await waitFor(() => expect(screen.getByText("conversation-1")).toBeVisible());
  });

  it("persists open and clear operations for the current identity", async () => {
    render(
      <AppShellProvider>
        <ActiveConversationProbe />
      </AppShellProvider>
    );

    fireEvent.click(screen.getByRole("button", { name: "open" }));
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("conversation-2");

    fireEvent.click(screen.getByRole("button", { name: "clear" }));
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});
