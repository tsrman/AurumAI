import { render, screen, fireEvent, within } from "@testing-library/react";
import { vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { ThemeProvider } from "@/providers/ThemeProvider";
import { useAppShell } from "@/hooks/useAppShell";
import { AppShell } from "@/components/aurum/AppShell";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/chat",
}));

const mockLogout = vi.fn();
let authRole = "org_admin";
vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({
    isAuthenticated: true,
    isLoading: false,
    user: { userId: "u1", email: "user@example.com", fullName: "Test User", role: authRole },
    logout: mockLogout,
  }),
}));

vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));
vi.mock("@/hooks/useHistory", () => ({
  useHistory: () => ({
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
  }),
}));

beforeEach(() => {
  authRole = "org_admin";
});

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <AppShellProvider>{children}</AppShellProvider>
    </ThemeProvider>
  );
}

function ActiveAgentSpy() {
  const { activeAgentId } = useAppShell();
  return <span data-testid="active-agent">{activeAgentId}</span>;
}

test("renders sidebar shell, main area and navigation structure", () => {
  render(
    <Wrapper>
      <AppShell>
        <div data-testid="main-content">Chat content</div>
      </AppShell>
    </Wrapper>
  );

  expect(screen.getByText("AurumAI")).toBeInTheDocument();
  expect(screen.getByText("New chat")).toBeInTheDocument();
  expect(screen.getByTestId("main-content")).toBeInTheDocument();

  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  expect(nav).toBeInTheDocument();
  expect(within(nav).getByText("AGENTS")).toBeInTheDocument();
  expect(within(nav).getByText("STUDIO")).toBeInTheDocument();

  // Agent list renders built-in agents as buttons
  expect(within(nav).getAllByRole("button")).toHaveLength(5);

  // Studio links render as anchors
  const studioLinks = within(nav).getAllByRole("link");
  expect(studioLinks).toHaveLength(3);
  expect(studioLinks[0]).toHaveAttribute("href", "/knowledge");
});

test("toggles side mini mode and hides labels", () => {
  render(
    <Wrapper>
      <AppShell>
        <div data-testid="main-content">Chat content</div>
      </AppShell>
    </Wrapper>
  );

  const sidebar = screen.getByLabelText("Sidebar");
  expect(sidebar).toHaveClass("w-[264px]");
  expect(sidebar).not.toHaveClass("w-[68px]");
  expect(screen.getByText("AurumAI")).toBeInTheDocument();
  expect(screen.getByText("AGENTS")).toBeInTheDocument();
  expect(screen.getByText("STUDIO")).toBeInTheDocument();

  fireEvent.click(screen.getByLabelText("Collapse"));

  expect(sidebar).toHaveClass("w-[68px]");
  expect(sidebar).not.toHaveClass("w-[264px]");
  expect(screen.queryByText("AurumAI")).not.toBeInTheDocument();
  expect(screen.queryByText("AGENTS")).not.toBeInTheDocument();
  expect(screen.queryByText("STUDIO")).not.toBeInTheDocument();
  expect(screen.queryByText("New chat")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Expand")).toBeInTheDocument();

  // Mini-mode nav items keep accessible names via aria-label
  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  for (const control of within(nav).getAllByRole("button")) {
    expect(control).toHaveAttribute("aria-label");
  }
  for (const control of within(nav).getAllByRole("link")) {
    expect(control).toHaveAttribute("aria-label");
  }
});

test("clicking an agent sets it as active", () => {
  render(
    <Wrapper>
      <AppShell>
        <ActiveAgentSpy />
      </AppShell>
    </Wrapper>
  );

  expect(screen.getByTestId("active-agent")).toHaveTextContent("general");

  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  const agentButtons = within(nav).getAllByRole("button");
  expect(agentButtons.length).toBeGreaterThanOrEqual(2);

  fireEvent.click(agentButtons[1]);

  expect(screen.getByTestId("active-agent")).toHaveTextContent("analyst");
});

test("ordinary members see server-filtered resource pages but not admin navigation", () => {
  authRole = "member";

  render(
    <Wrapper>
      <AppShell>
        <div data-testid="main-content">Chat content</div>
      </AppShell>
    </Wrapper>
  );

  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  const studioLinks = within(nav).getAllByRole("link");

  expect(studioLinks).toHaveLength(2);
  expect(studioLinks[0]).toHaveAttribute("href", "/knowledge");
  expect(studioLinks[1]).toHaveAttribute("href", "/history");
});

test("resource navigation does not depend on client-side permission strings", () => {
  authRole = "member";

  render(
    <Wrapper>
      <AppShell>
        <div data-testid="main-content">Chat content</div>
      </AppShell>
    </Wrapper>
  );

  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  const hrefs = within(nav)
    .getAllByRole("link")
    .map((link) => link.getAttribute("href"));

  expect(hrefs).toContain("/knowledge");
  expect(hrefs).toContain("/history");
  expect(hrefs.some((h) => h?.includes("/admin/"))).toBe(false);
});

test("Administration points at the external admin-ui app for an org admin", () => {
  authRole = "org_admin";

  render(
    <Wrapper>
      <AppShell>
        <div data-testid="main-content">Chat content</div>
      </AppShell>
    </Wrapper>
  );

  const nav = screen.getByRole("navigation", { name: "Main navigation" });
  const hrefs = within(nav)
    .getAllByRole("link")
    .map((link) => link.getAttribute("href"));

  expect(hrefs).toContain("http://localhost:3100/admin/users");
});
