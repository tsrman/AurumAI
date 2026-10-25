import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import AdminAnalyticsPage from "@/app/admin/analytics/page";
import type { AdminUsageReport, AdminUser } from "@/lib/api";

const replace = vi.fn();
let params = new URLSearchParams(
  "from=2026-09-01&to=2026-09-03&userId=user-1&model=provider%2Fmodel"
);
let access = {
  allowed: true,
  canWrite: true,
  error: null as string | null,
  isLoading: false,
};
const useAdminUsage = vi.fn();
const useAdminUsers = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => params,
}));
vi.mock("@/hooks/useAdminAccess", () => ({ useAdminAccess: () => access }));
vi.mock("@/hooks/useAdminAnalytics", () => ({
  useAdminUsage: (filters: unknown) => useAdminUsage(filters),
  useAdminUsers: (filters: unknown) => useAdminUsers(filters),
}));
vi.mock("@/components/analytics/UsageChart", () => ({
  UsageChart: ({ metric }: { metric: string }) => <div>chart:{metric}</div>,
}));

const report: AdminUsageReport = {
  period: { from: "2026-09-01", to: "2026-09-03", timezone: "UTC" },
  limits: { requestLimit: 10, tokenLimit: 100 },
  quota: {
    periodStart: "2026-09-01",
    requestsUsed: 8,
    tokensUsed: 90,
    requestsRemaining: 2,
    tokensRemaining: 10,
  },
  totals: {
    requests: 3,
    promptTokens: 7,
    completionTokens: 5,
    totalTokens: 12,
    unaccountedRequests: 1,
    images: 2,
  },
  daily: [],
  byUser: [
    {
      userId: "user-1",
      email: "admin@example.com",
      fullName: "Admin",
      requests: 3,
      promptTokens: 7,
      completionTokens: 5,
      totalTokens: 12,
      unaccountedRequests: 1,
      images: 2,
    },
  ],
  byUserTotal: 1,
  byModel: [
    {
      model: "provider/model",
      requests: 3,
      promptTokens: 7,
      completionTokens: 5,
      totalTokens: 12,
      unaccountedRequests: 1,
      images: 2,
    },
  ],
};
const users: AdminUser[] = [
  {
    id: "user-1",
    email: "admin@example.com",
    fullName: "Admin",
    role: "org_admin",
    status: "active",
    lastLoginAt: null,
    createdAt: "2026-09-01T00:00:00Z",
  },
];

beforeEach(() => {
  replace.mockReset();
  params = new URLSearchParams(
    "from=2026-09-01&to=2026-09-03&userId=user-1&model=provider%2Fmodel"
  );
  access = { allowed: true, canWrite: true, error: null, isLoading: false };
  useAdminUsage.mockReset().mockReturnValue({
    data: { data: report },
    isLoading: false,
    error: null,
  });
  useAdminUsers.mockReset().mockReturnValue({
    data: { data: users, pagination: { total: 75, hasMore: true } },
    isLoading: false,
    error: null,
  });
});

test("reads report filters from URL and renders totals, limits and breakdowns", () => {
  render(<AdminAnalyticsPage />);

  expect(useAdminUsage).toHaveBeenCalledWith({
    from: "2026-09-01",
    to: "2026-09-03",
    userId: "user-1",
    model: "provider/model",
  });
  const totals = screen.getByRole("region", { name: "Totals" });
  expect(within(totals).getByText("3")).toBeInTheDocument();
  expect(within(totals).getByText("12")).toBeInTheDocument();
  expect(within(totals).getByText("Images").nextSibling).toHaveTextContent("2");
  // Image counts sit next to requests and tokens in both breakdowns.
  expect(screen.getAllByRole("columnheader", { name: "Images" })).toHaveLength(2);
  // From the monthly counter (8/90 used), not from the three-day period's
  // own totals — those would have read 7 and 88.
  expect(screen.getByText(/Requests remaining: 2/)).toBeInTheDocument();
  expect(screen.getByText(/Tokens remaining: 10/)).toBeInTheDocument();
  expect(screen.getByText(/1 request without token data/)).toBeInTheDocument();
  expect(screen.getByText("provider/model")).toBeInTheDocument();
  expect(screen.getByText("admin@example.com")).toBeInTheDocument();
});

test("submits URL filters and resets user pagination when filters change", () => {
  render(<AdminAnalyticsPage />);

  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(useAdminUsers).toHaveBeenLastCalledWith({ limit: 50, offset: 50 });
  fireEvent.change(screen.getByLabelText("User"), {
    target: { value: "" },
  });
  expect(useAdminUsers).toHaveBeenLastCalledWith({ limit: 50, offset: 0 });
  fireEvent.change(screen.getByLabelText("Model"), {
    target: { value: "" },
  });
  fireEvent.submit(screen.getByRole("form", { name: "Analytics filters" }));

  expect(replace).toHaveBeenCalledWith(
    "/admin/analytics?from=2026-09-01&to=2026-09-03"
  );
});

test("covers access, loading, validation error, and empty report states", () => {
  access = { allowed: false, canWrite: false, error: null, isLoading: true };
  const { rerender } = render(<AdminAnalyticsPage />);
  expect(screen.getByText("Checking access...")).toBeInTheDocument();

  access = { allowed: false, canWrite: false, error: "Administrators only", isLoading: false };
  rerender(<AdminAnalyticsPage />);
  expect(screen.getByText("Administrators only")).toBeInTheDocument();

  access = { allowed: true, canWrite: true, error: null, isLoading: false };
  useAdminUsage.mockReturnValue({ data: undefined, isLoading: true, error: null });
  rerender(<AdminAnalyticsPage />);
  expect(screen.getByText("Loading analytics...")).toBeInTheDocument();

  useAdminUsage.mockReturnValue({ data: undefined, isLoading: false, error: new Error("invalid range") });
  rerender(<AdminAnalyticsPage />);
  expect(screen.getByText("invalid range")).toBeInTheDocument();
});

test("renders unlimited quotas without a numeric remaining value", () => {
  useAdminUsage.mockReturnValue({
    data: {
      data: {
        ...report,
        limits: { requestLimit: null, tokenLimit: null },
        quota: {
          ...report.quota,
          requestsRemaining: null,
          tokensRemaining: null,
        },
      },
    },
    isLoading: false,
    error: null,
  });

  render(<AdminAnalyticsPage />);

  expect(screen.getAllByText("Unlimited")).toHaveLength(2);
  expect(screen.queryByText(/Requests remaining:/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Tokens remaining:/)).not.toBeInTheDocument();
});

test("synchronizes filter controls when browser navigation changes the URL", () => {
  const { rerender } = render(<AdminAnalyticsPage />);
  params = new URLSearchParams("from=2026-08-01&to=2026-08-31");

  rerender(<AdminAnalyticsPage />);

  expect(screen.getByLabelText("From")).toHaveValue("2026-08-01");
  expect(screen.getByLabelText("To")).toHaveValue("2026-08-31");
  expect(screen.getByLabelText("User")).toHaveValue("");
  expect(screen.getByLabelText("Model")).toHaveValue("");
});

test("shows an actionable message for backend filter validation", () => {
  useAdminUsage.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: { response: new Response(null, { status: 422 }) },
  });

  render(<AdminAnalyticsPage />);

  expect(
    screen.getByText("Check the analytics period and filters.")
  ).toBeInTheDocument();
});

test("ranks the per-user table by the report, not by the user directory page", () => {
  // The directory is ordered and paginated for the filter dropdown, so
  // walking it put the biggest consumers on whatever page they happened to
  // land — and users with no usage at all ahead of them.
  useAdminUsage.mockReturnValue({
    data: {
      data: {
        ...report,
        byUser: [
          { ...report.byUser[0], userId: "user-2", email: "heavy@example.com", fullName: "Heavy", totalTokens: 900 },
          { ...report.byUser[0], userId: "user-1", email: "light@example.com", fullName: "Light", totalTokens: 12 },
        ],
      },
    },
    isLoading: false,
    error: null,
  });
  useAdminUsers.mockReturnValue({
    data: { data: [users[0]], pagination: { total: 75, hasMore: true } },
    isLoading: false,
    error: null,
  });

  render(<AdminAnalyticsPage />);

  const rows = screen
    .getAllByRole("row")
    .map((row) => row.textContent ?? "")
    .filter((text) => text.includes("@example.com"));
  expect(rows[0]).toContain("heavy@example.com");
  expect(rows[1]).toContain("light@example.com");
  // The directory's own row is only an option in the filter, never a table row.
  expect(rows.some((text) => text.includes("admin@example.com"))).toBe(false);
});

test("says so when the per-user table is capped by the backend", () => {
  useAdminUsage.mockReturnValue({
    data: { data: { ...report, byUserTotal: 137 } },
    isLoading: false,
    error: null,
  });

  const { rerender } = render(<AdminAnalyticsPage />);
  expect(
    screen.getByText(/Showing 1 of 137 users with usage/)
  ).toBeInTheDocument();

  // An uncapped list must not carry the notice.
  useAdminUsage.mockReturnValue({
    data: { data: { ...report, byUserTotal: 1 } },
    isLoading: false,
    error: null,
  });
  rerender(<AdminAnalyticsPage />);
  expect(screen.queryByText(/users with usage/)).not.toBeInTheDocument();
});
