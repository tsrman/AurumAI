import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { Sidebar } from "../Sidebar";
import { useHistory } from "@/hooks/useHistory";
import { useAppShell } from "@/hooks/useAppShell";
import { useAuth } from "@/providers/AuthProvider";
import { useRouter, usePathname } from "next/navigation";
import { ThemeProvider } from "@/providers/ThemeProvider";
import type { Conversation } from "@/types/aurum";

vi.mock("@/hooks/useHistory");
vi.mock("@/hooks/useAppShell");
vi.mock("@/providers/AuthProvider");
vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
  usePathname: vi.fn(),
}));

const mockLogout = vi.fn();

const mockPush = vi.fn();
const mockToggleSide = vi.fn();
const mockSetActiveAgentId = vi.fn();
const mockSetActiveConversationId = vi.fn();

function createConversation(id: string, title: string, time = "10:00"): Conversation {
  return {
    id,
    agentId: "general",
    title,
    date: "Today",
    time,
    messages: [],
    model: "gpt-4o-mini", temperature: 0.7,
    lastMessageAt: new Date().toISOString(),
    status: "active",
    messageCount: 1,
  };
}

function mockUseHistory(
  overrides: Partial<ReturnType<typeof useHistory>> = {}
): void {
  vi.mocked(useHistory).mockReturnValue({
    historyItems: [],
    groups: [],
    isLoading: false,
    isError: false,
    error: null,
    hasNextPage: false,
    fetchNextPage: vi.fn(),
    isFetchingNextPage: false,
    refetch: vi.fn(),
    openConversation: vi.fn(),
    renameConversation: vi.fn(),
    deleteConversation: vi.fn(),
    isRenaming: false,
    isDeleting: false,
    ...overrides,
  } as unknown as ReturnType<typeof useHistory>);
}

function renderSidebar() {
  return render(
    <ThemeProvider>
      <Sidebar />
    </ThemeProvider>
  );
}

describe("Sidebar", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();

    vi.mocked(useRouter).mockReturnValue({
      push: mockPush,
    } as unknown as ReturnType<typeof useRouter>);
    vi.mocked(usePathname).mockReturnValue("/chat");

    vi.mocked(useAppShell).mockReturnValue({
      sideMini: false,
      toggleSide: mockToggleSide,
      agents: [],
      myAgents: [],
      activeAgentId: "general",
      setActiveAgentId: mockSetActiveAgentId,
      setActiveConversationId: mockSetActiveConversationId,
    } as unknown as ReturnType<typeof useAppShell>);

    vi.mocked(useAuth).mockReturnValue({
      user: {
        userId: "u1",
        email: "gromov@example.com",
        fullName: "D. A. Gromov",
        role: "org_admin",
        token: "t",
      },
      token: "t",
      isAuthenticated: true,
      isLoading: false,
      login: vi.fn(),
      logout: mockLogout,
    } as unknown as ReturnType<typeof useAuth>);

    mockUseHistory();
  });

  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  it("renders the conversation list from useHistory", () => {
    const conversations = [
      createConversation("conv-1", "First chat"),
      createConversation("conv-2", "Second chat", "11:00"),
    ];

    mockUseHistory({
      historyItems: [{ date: "Today", conversations }],
    });

    renderSidebar();

    expect(screen.getByText("HISTORY")).toBeInTheDocument();
    expect(screen.getByText("First chat")).toBeInTheDocument();
    expect(screen.getByText("Second chat")).toBeInTheDocument();
    expect(screen.getByText("10:00")).toBeInTheDocument();
    expect(screen.getByText("11:00")).toBeInTheDocument();
  });

  it("calls openConversation when a conversation is clicked", () => {
    const openConversation = vi.fn();
    const conversations = [createConversation("conv-1", "First chat")];

    mockUseHistory({
      historyItems: [{ date: "Today", conversations }],
      openConversation,
    });

    renderSidebar();

    fireEvent.click(screen.getByText("First chat"));
    expect(openConversation).toHaveBeenCalledWith("conv-1");
  });

  it("calls renameConversation from the item action menu", async () => {
    const renameConversation = vi.fn();
    const conversations = [createConversation("conv-1", "First chat")];

    mockUseHistory({
      historyItems: [{ date: "Today", conversations }],
      renameConversation,
    });

    renderSidebar();

    fireEvent.click(screen.getByLabelText("Actions"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Rename/i }));

    const input = screen.getByLabelText("Conversation title");
    fireEvent.change(input, { target: { value: "Renamed chat" } });
    fireEvent.blur(input);

    await waitFor(() => {
      expect(renameConversation).toHaveBeenCalledWith("conv-1", "Renamed chat");
    });
  });

  it("calls deleteConversation after confirming in the dialog", () => {
    const deleteConversation = vi.fn();
    const conversations = [createConversation("conv-1", "First chat")];

    mockUseHistory({
      historyItems: [{ date: "Today", conversations }],
      deleteConversation,
    });

    renderSidebar();

    fireEvent.click(screen.getByLabelText("Actions"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete/i }));

    // Regression: this used to be the native window.confirm() — now a
    // ConfirmDialog rendered in the app's own UI.
    const dialog = screen.getByRole("alertdialog", { name: /Delete the conversation\?/i });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(deleteConversation).toHaveBeenCalledWith("conv-1");
  });

  it("does not call deleteConversation when the confirm dialog is cancelled", () => {
    const deleteConversation = vi.fn();
    const conversations = [createConversation("conv-1", "First chat")];

    mockUseHistory({
      historyItems: [{ date: "Today", conversations }],
      deleteConversation,
    });

    renderSidebar();

    fireEvent.click(screen.getByLabelText("Actions"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete/i }));

    const dialog = screen.getByRole("alertdialog", { name: /Delete the conversation\?/i });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(deleteConversation).not.toHaveBeenCalled();
  });

  it("shows a loading state", () => {
    mockUseHistory({
      isLoading: true,
      historyItems: [],
    });

    renderSidebar();

    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  it("shows an error state", () => {
    mockUseHistory({
      isError: true,
      error: new Error("Network error"),
      historyItems: [],
    });

    renderSidebar();

    expect(screen.getByText("Loading failed")).toBeInTheDocument();
    expect(screen.getByText("Network error")).toBeInTheDocument();
  });

  it("shows an empty state when there are no conversations", () => {
    mockUseHistory({
      historyItems: [],
    });

    renderSidebar();

    expect(screen.getByText("No recent conversations.")).toBeInTheDocument();
  });

  it("resets the active conversation and navigates to /chat when 'New chat' is clicked", () => {
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: "New chat" }));

    // Regression: previously only router.push("/chat") was called, which is a
    // no-op when already on /chat and never cleared the previously open
    // conversation — the button appeared to do nothing.
    expect(mockSetActiveConversationId).toHaveBeenCalledWith(null);
    expect(mockPush).toHaveBeenCalledWith("/chat");
  });

  it("shows the real authenticated user, not a hardcoded name, and wires logout", () => {
    renderSidebar();

    // Sidebar previously showed a hardcoded
    // USER_NAME/USER_ROLE regardless of who was logged in.
    expect(screen.getByText("D. A. Gromov")).toBeInTheDocument();
    expect(screen.getByText("org_admin")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Sign out"));
    expect(mockLogout).toHaveBeenCalledTimes(1);
  });

  it("falls back to email, then a generic label, when fullName/role are missing", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: {
        userId: "u2",
        email: "noname@example.com",
        role: "",
        token: "t",
      },
      token: "t",
      isAuthenticated: true,
      isLoading: false,
      login: vi.fn(),
      logout: mockLogout,
    } as unknown as ReturnType<typeof useAuth>);

    renderSidebar();

    expect(screen.getByText("noname@example.com")).toBeInTheDocument();
  });

  it("hides the history section in mini mode", () => {
    vi.mocked(useAppShell).mockReturnValue({
      sideMini: true,
      toggleSide: mockToggleSide,
      agents: [],
      myAgents: [],
      activeAgentId: "general",
      setActiveAgentId: mockSetActiveAgentId,
      setActiveConversationId: mockSetActiveConversationId,
    } as unknown as ReturnType<typeof useAppShell>);

    mockUseHistory({
      historyItems: [
        { date: "Today", conversations: [createConversation("conv-1", "First chat")] },
      ],
    });

    renderSidebar();

    expect(screen.queryByText("HISTORY")).not.toBeInTheDocument();
    expect(screen.queryByText("First chat")).not.toBeInTheDocument();
  });
});
