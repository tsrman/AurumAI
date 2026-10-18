import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { HistoryList } from "@/components/aurum/history/HistoryList";
import { useHistory, type UseHistoryReturn } from "@/hooks/useHistory";
import { useAppShell } from "@/hooks/useAppShell";
import type { Agent, Conversation } from "@/types/aurum";

vi.mock("@/hooks/useHistory", () => ({
  useHistory: vi.fn(),
}));

vi.mock("@/hooks/useAppShell", () => ({
  useAppShell: vi.fn(),
}));

const mockOpenConversation = vi.fn();
const mockFetchNextPage = vi.fn();
const mockRefetch = vi.fn();

const agent: Agent = {
  id: "analyst",
  name: "Analyst",
  badge: "AN",
  badgeColor: "blue",
  kind: "system",
  kindLabel: "System",
  description: "",
  capabilities: [],
  suggestions: [],
};

function makeConversation(
  id: string,
  title: string,
  date: string
): Conversation {
  return {
    id,
    agentId: agent.id,
    title,
    date,
    time: "09:48",
    messages: [],
    model: "pro", temperature: 0.7,
    lastMessageAt: new Date().toISOString(),
    status: "active",
    messageCount: 2,
  };
}

function createUseHistoryMock(
  overrides: Partial<UseHistoryReturn> = {}
): UseHistoryReturn {
  return {
    groups: [],
    historyItems: [],
    isLoading: false,
    isError: false,
    error: null,
    hasNextPage: false,
    fetchNextPage: mockFetchNextPage,
    isFetchingNextPage: false,
    refetch: mockRefetch,
    openConversation: mockOpenConversation,
    renameConversation: vi.fn(),
    deleteConversation: vi.fn(),
    isRenaming: false,
    isDeleting: false,
    ...overrides,
  };
}

class IntersectionObserverMock {
  callback: IntersectionObserverCallback;
  observed: Element[] = [];

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }

  observe(target: Element) {
    this.observed.push(target);
  }

  unobserve(target: Element) {
    this.observed = this.observed.filter((el) => el !== target);
  }

  disconnect() {
    this.observed = [];
  }

  simulate(isIntersecting: boolean) {
    const entries: IntersectionObserverEntry[] = this.observed.map(
      (target) =>
        ({
          isIntersecting,
          target,
          boundingClientRect: {} as DOMRectReadOnly,
          intersectionRatio: isIntersecting ? 1 : 0,
          intersectionRect: {} as DOMRectReadOnly,
          rootBounds: null,
          time: Date.now(),
        } as unknown as IntersectionObserverEntry)
    );
    this.callback(entries, this as unknown as IntersectionObserver);
  }
}

let ioInstances: IntersectionObserverMock[] = [];

describe("HistoryList", () => {
  beforeEach(() => {
    ioInstances = [];
    // A regular `function`, not an arrow function: HistoryList calls
    // `new IntersectionObserver(...)`, and vitest's mock proxy now actually
    // invokes the implementation via `new` — arrow functions are never
    // constructible.
    globalThis.IntersectionObserver = vi.fn(function (callback) {
      const instance = new IntersectionObserverMock(callback);
      ioInstances.push(instance);
      return instance as unknown as IntersectionObserver;
    }) as unknown as typeof IntersectionObserver;

    vi.mocked(useAppShell).mockReturnValue({
      agents: [agent],
      myAgents: [],
    } as unknown as ReturnType<typeof useAppShell>);

    vi.mocked(useHistory).mockReturnValue(createUseHistoryMock());
    mockOpenConversation.mockClear();
    mockFetchNextPage.mockClear();
    mockRefetch.mockClear();
  });

  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
  });

  it("renders grouped history items", () => {
    vi.mocked(useHistory).mockReturnValue(
      createUseHistoryMock({
        historyItems: [
          {
            date: "Today",
            conversations: [makeConversation("c1", "Extraction today", "Today")],
          },
          {
            date: "Yesterday",
            conversations: [
              makeConversation("c2", "pH requirements", "Yesterday"),
              makeConversation("c3", "Top downtime", "Yesterday"),
            ],
          },
        ],
      })
    );

    render(<HistoryList />);

    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("Yesterday")).toBeInTheDocument();

    const todaySection = screen.getByText("Today").closest("section");
    const yesterdaySection = screen.getByText("Yesterday").closest("section");

    expect(todaySection).toBeInTheDocument();
    expect(yesterdaySection).toBeInTheDocument();

    expect(
      within(todaySection!).getAllByRole("button", { name: /Open conversation/i })
    ).toHaveLength(1);
    expect(
      within(yesterdaySection!).getAllByRole("button", {
        name: /Open conversation/i,
      })
    ).toHaveLength(2);

    expect(screen.getByText("Extraction today")).toBeInTheDocument();
    expect(screen.getByText("pH requirements")).toBeInTheDocument();
    expect(screen.getByText("Top downtime")).toBeInTheDocument();
    expect(screen.getAllByText("AN")).toHaveLength(3);
    expect(screen.getAllByText("2 messages")).toHaveLength(3);
  });

  it("calls openConversation when a conversation is clicked", () => {
    vi.mocked(useHistory).mockReturnValue(
      createUseHistoryMock({
        historyItems: [
          {
            date: "Today",
            conversations: [makeConversation("c1", "Extraction today", "Today")],
          },
        ],
      })
    );

    render(<HistoryList />);

    fireEvent.click(screen.getByText("Extraction today"));
    expect(mockOpenConversation).toHaveBeenCalledWith("c1");
  });

  it("shows a loading state when data is loading", () => {
    vi.mocked(useHistory).mockReturnValue(
      createUseHistoryMock({ isLoading: true })
    );

    render(<HistoryList />);

    expect(screen.getByText("Loading history...")).toBeInTheDocument();
  });

  it("shows an empty state when there are no conversations", () => {
    render(<HistoryList />);

    expect(
      screen.getByText("Conversation history is empty.")
    ).toBeInTheDocument();
  });

  it("shows an error state with a retry button", () => {
    vi.mocked(useHistory).mockReturnValue(
      createUseHistoryMock({
        isError: true,
        error: new Error("Network failure"),
      })
    );

    render(<HistoryList />);

    expect(
      screen.getByText("Could not load history.")
    ).toBeInTheDocument();
    expect(screen.getByText("Network failure")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Retry/i }));
    expect(mockRefetch).toHaveBeenCalled();
  });

  it("observes the sentinel and fetches the next page when it intersects", () => {
    vi.mocked(useHistory).mockReturnValue(
      createUseHistoryMock({
        hasNextPage: true,
        historyItems: [
          {
            date: "Today",
            conversations: [makeConversation("c1", "Extraction today", "Today")],
          },
        ],
      })
    );

    render(<HistoryList />);

    const sentinel = screen.getByTestId("history-sentinel");
    expect(sentinel).toBeInTheDocument();
    expect(ioInstances).toHaveLength(1);
    expect(ioInstances[0].observed).toContain(sentinel);

    ioInstances[0].simulate(true);
    expect(mockFetchNextPage).toHaveBeenCalled();
  });

  it("does not create an observer when there is no next page", () => {
    vi.mocked(useHistory).mockReturnValue(
      createUseHistoryMock({
        hasNextPage: false,
        historyItems: [
          {
            date: "Today",
            conversations: [makeConversation("c1", "Extraction today", "Today")],
          },
        ],
      })
    );

    render(<HistoryList />);

    expect(screen.queryByTestId("history-sentinel")).not.toBeInTheDocument();
    expect(ioInstances).toHaveLength(0);
  });

  it("does not create an observer while the next page is already fetching", () => {
    vi.mocked(useHistory).mockReturnValue(
      createUseHistoryMock({
        hasNextPage: true,
        isFetchingNextPage: true,
        historyItems: [
          {
            date: "Today",
            conversations: [makeConversation("c1", "Extraction today", "Today")],
          },
        ],
      })
    );

    render(<HistoryList />);

    expect(screen.getByTestId("history-sentinel")).toBeInTheDocument();
    expect(ioInstances).toHaveLength(0);
  });
});
