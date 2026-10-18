import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { HistoryItem } from "../HistoryItem";
import { useHistory } from "@/hooks/useHistory";
import type { Conversation, Agent } from "@/types/aurum";

vi.mock("@/hooks/useHistory");

const mockRenameConversation = vi.fn();
const mockDeleteConversation = vi.fn();

const mockUseHistory = (overrides: Partial<ReturnType<typeof useHistory>> = {}) => {
  vi.mocked(useHistory).mockReturnValue({
    renameConversation: mockRenameConversation,
    deleteConversation: mockDeleteConversation,
    isRenaming: false,
    isDeleting: false,
    ...overrides,
  } as unknown as ReturnType<typeof useHistory>);
};

const agent: Agent = {
  id: "general",
  name: "General Agent",
  badge: "AG",
  badgeColor: "blue",
  kind: "system",
  kindLabel: "System",
  description: "",
  capabilities: [],
  suggestions: [],
};

const conversation: Conversation = {
  id: "conv-1",
  agentId: "general",
  title: "Test dialog",
  date: "Today",
  time: "10:00",
  messages: [
    { id: "msg-1", role: "user", content: "Hello there" },
    { id: "msg-2", role: "assistant", agentId: "general", thinking: "", tools: [], content: "Hi", widgets: [], sources: [], thinkingShown: "", contentShown: "Hi", phase: "done", thinkingOpen: false, thinkingSeconds: 0, startedAt: Date.now() },
  ],
  model: "gpt-4o-mini", temperature: 0.7,
  lastMessageAt: new Date().toISOString(),
  status: "active",
  messageCount: 2,
};

describe("HistoryItem", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    mockUseHistory();
  });

  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  it("renders title, time and message count", () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    expect(screen.getByText("Test dialog")).toBeInTheDocument();
    expect(screen.getByText("10:00")).toBeInTheDocument();
    expect(screen.getByText("2 messages")).toBeInTheDocument();
    expect(screen.getByText("AG")).toBeInTheDocument();
  });

  it("renders the calendar date next to the time, not just the time", () => {
    // Regression: the card used to show only the relative time (e.g.
    // "10:00"), with no indication of which day it was on — see date.ts
    // formatShortDate.
    const withKnownDate: Conversation = {
      ...conversation,
      lastMessageAt: "2026-07-11T08:53:00.000Z",
    };
    render(<HistoryItem conversation={withKnownDate} agent={agent} onClick={vi.fn()} />);

    expect(screen.getByText("07/11")).toBeInTheDocument();
  });

  it("renders last user message snippet", () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);
    expect(screen.getByText("Hello there")).toBeInTheDocument();
  });

  it("calls onClick when conversation body is clicked", () => {
    const onClick = vi.fn();
    render(<HistoryItem conversation={conversation} agent={agent} onClick={onClick} />);

    fireEvent.click(screen.getByText("Test dialog"));
    expect(onClick).toHaveBeenCalled();
  });

  it("enters rename mode from menu and calls renameConversation on blur", async () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    fireEvent.click(screen.getByLabelText("Actions"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Rename/i }));

    const input = screen.getByLabelText("Conversation title");
    expect(input).toHaveValue("Test dialog");

    fireEvent.change(input, { target: { value: "New title" } });
    fireEvent.blur(input);

    await waitFor(() => {
      expect(mockRenameConversation).toHaveBeenCalledWith("conv-1", "New title");
    });
  });

  it("enters rename mode on double-click and saves on Enter", async () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    fireEvent.doubleClick(screen.getByText("Test dialog"));

    const input = screen.getByLabelText("Conversation title");
    fireEvent.change(input, { target: { value: "Updated title" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(mockRenameConversation).toHaveBeenCalledWith("conv-1", "Updated title");
    });
  });

  it("cancels rename on Escape and reverts the title", () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    fireEvent.doubleClick(screen.getByText("Test dialog"));

    const input = screen.getByLabelText("Conversation title");
    fireEvent.change(input, { target: { value: "Draft title" } });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(mockRenameConversation).not.toHaveBeenCalled();
    expect(screen.getByText("Test dialog")).toBeInTheDocument();
  });

  it("cancels rename when trimmed title is empty", () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    fireEvent.doubleClick(screen.getByText("Test dialog"));

    const input = screen.getByLabelText("Conversation title");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.blur(input);

    expect(mockRenameConversation).not.toHaveBeenCalled();
    expect(screen.getByText("Test dialog")).toBeInTheDocument();
  });

  it("calls deleteConversation after confirming in the dialog", () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    fireEvent.click(screen.getByLabelText("Actions"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete/i }));

    // Regression: this used to be the native window.confirm(), which can't
    // be styled or driven in screenshot-based verification — now a
    // ConfirmDialog rendered in the app's own UI.
    const dialog = screen.getByRole("alertdialog", { name: /Delete the conversation\?/i });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(mockDeleteConversation).toHaveBeenCalledWith("conv-1");
  });

  it("does not call deleteConversation when the confirm dialog is cancelled", () => {
    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    fireEvent.click(screen.getByLabelText("Actions"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete/i }));

    const dialog = screen.getByRole("alertdialog", { name: /Delete the conversation\?/i });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(mockDeleteConversation).not.toHaveBeenCalled();
  });

  it("disables input while rename is pending", () => {
    mockUseHistory({ isRenaming: true });

    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    fireEvent.doubleClick(screen.getByText("Test dialog"));
    const input = screen.getByLabelText("Conversation title");
    expect(input).toBeDisabled();
  });

  it("shows pending spinner and disables main button while deleting", () => {
    mockUseHistory({ isDeleting: true });

    render(<HistoryItem conversation={conversation} agent={agent} onClick={vi.fn()} />);

    expect(screen.getByLabelText("Actions")).toBeDisabled();
    expect(document.querySelector("svg.animate-spin")).toBeInTheDocument();
  });
});
