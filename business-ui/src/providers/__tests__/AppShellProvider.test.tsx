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

function ModelProbe() {
  const { activeModelId, setActiveModelId } = useAppShell();
  return (
    <>
      <output data-testid="selected-model">{activeModelId}</output>
      <button onClick={() => setActiveModelId("deepseek-flash-free")}>select model</button>
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

describe("AppShellProvider model recovery", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      user: { userId: "user-1", orgId: "org-1" },
    } as ReturnType<typeof useAuth>);
    vi.mocked(useModels).mockReturnValue({
      data: { data: [{ id: "MiniMax-M3" }, { id: "deepseek-flash-free" }] },
    } as ReturnType<typeof useModels>);
    vi.mocked(useSettings).mockReturnValue({
      data: { data: { default_model: "MiniMax-M3" } },
      isPending: false,
    } as ReturnType<typeof useSettings>);
  });

  it("keeps the manually selected model after a reload", async () => {
    const first = render(<AppShellProvider><ModelProbe /></AppShellProvider>);
    fireEvent.click(screen.getByRole("button", { name: "select model" }));
    first.unmount();

    render(<AppShellProvider><ModelProbe /></AppShellProvider>);

    await waitFor(() => expect(screen.getByTestId("selected-model"))
      .toHaveTextContent("deepseek-flash-free"));
  });

  it("does not expose the fallback while an authenticated selection is restoring", async () => {
    const first = render(<AppShellProvider><ModelProbe /></AppShellProvider>);
    fireEvent.click(screen.getByRole("button", { name: "select model" }));
    first.unmount();
    const signedIn = vi.mocked(useAuth).mock.results.at(-1)!.value;
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: false, isLoading: true,
    } as ReturnType<typeof useAuth>);
    vi.mocked(useModels).mockReturnValue({ data: undefined } as ReturnType<typeof useModels>);
    const view = render(<AppShellProvider><ModelProbe /></AppShellProvider>);

    vi.mocked(useAuth).mockReturnValue(signedIn);
    view.rerender(<AppShellProvider><ModelProbe /></AppShellProvider>);
    expect(screen.getByTestId("selected-model")).toBeEmptyDOMElement();
    vi.mocked(useModels).mockReturnValue({
      data: { data: [{ id: "MiniMax-M3" }, { id: "deepseek-flash-free" }] },
    } as ReturnType<typeof useModels>);
    view.rerender(<AppShellProvider><ModelProbe /></AppShellProvider>);
    expect(screen.getByTestId("selected-model")).toBeEmptyDOMElement();
    await waitFor(() => expect(screen.getByTestId("selected-model"))
      .toHaveTextContent("deepseek-flash-free"));
  });

  it.each([
    { userId: "user-2", orgId: "org-1" },
    { userId: "user-1", orgId: "org-2" },
  ])("does not carry a model choice into another identity: %j", async (user) => {
    const view = render(<AppShellProvider><ModelProbe /></AppShellProvider>);
    fireEvent.click(screen.getByRole("button", { name: "select model" }));
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: true, isLoading: false, user,
    } as ReturnType<typeof useAuth>);

    view.rerender(<AppShellProvider><ModelProbe /></AppShellProvider>);

    await waitFor(() => expect(screen.getByTestId("selected-model"))
      .toHaveTextContent("MiniMax-M3"));
  });

  it("falls back when a selected model is no longer authorized", async () => {
    const view = render(<AppShellProvider><ModelProbe /></AppShellProvider>);
    fireEvent.click(screen.getByRole("button", { name: "select model" }));
    vi.mocked(useModels).mockReturnValue({
      data: { data: [{ id: "MiniMax-M3" }] },
    } as ReturnType<typeof useModels>);

    view.rerender(<AppShellProvider><ModelProbe /></AppShellProvider>);

    await waitFor(() => expect(screen.getByTestId("selected-model"))
      .toHaveTextContent("MiniMax-M3"));
  });
});
