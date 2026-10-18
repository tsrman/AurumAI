import { render } from "@testing-library/react";
import { vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { ThemeProvider } from "@/providers/ThemeProvider";
import { AppShell } from "@/components/aurum/AppShell";

const push = vi.fn();
let pathname = "/chat";
let authState = { isAuthenticated: true, isLoading: false };

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  usePathname: () => pathname,
}));

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => authState,
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

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <AppShellProvider>{children}</AppShellProvider>
    </ThemeProvider>
  );
}

beforeEach(() => {
  push.mockClear();
  pathname = "/chat";
  authState = { isAuthenticated: true, isLoading: false };
});

test("does not redirect an authenticated user", () => {
  render(
    <Wrapper>
      <AppShell>
        <div>content</div>
      </AppShell>
    </Wrapper>
  );
  expect(push).not.toHaveBeenCalled();
});

test("redirects an unauthenticated user from a protected route to /login", () => {
  authState = { isAuthenticated: false, isLoading: false };
  pathname = "/chat";
  render(
    <Wrapper>
      <AppShell>
        <div>content</div>
      </AppShell>
    </Wrapper>
  );
  expect(push).toHaveBeenCalledWith("/login");
});

test("does not redirect to /login while the session is loading", () => {
  authState = { isAuthenticated: false, isLoading: true };
  render(
    <Wrapper>
      <AppShell>
        <div>content</div>
      </AppShell>
    </Wrapper>
  );
  expect(push).not.toHaveBeenCalled();
});

test("does not redirect from /login (no redirect loop)", () => {
  authState = { isAuthenticated: false, isLoading: false };
  pathname = "/login";
  render(
    <Wrapper>
      <AppShell>
        <div>content</div>
      </AppShell>
    </Wrapper>
  );
  expect(push).not.toHaveBeenCalled();
});
