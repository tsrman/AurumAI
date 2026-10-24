import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import AdminUsersPage from "@/app/admin/users/page";
import type { AdminUser } from "@/lib/api";

let adminAccess = { allowed: true, canWrite: true, error: null as string | null, isLoading: false };
const usersQuery = vi.fn();
let result: { data?: { data: AdminUser[]; pagination?: { hasMore: boolean; total?: number } }; isLoading: boolean; error: unknown };

vi.mock("next/navigation", () => ({ usePathname: () => "/admin/users" }));
vi.mock("@/hooks/useAdminAccess", () => ({ useAdminAccess: () => adminAccess }));
vi.mock("@/hooks/useAdminAnalytics", () => ({
  useAdminUsers: (filters: unknown) => {
    usersQuery(filters);
    return result;
  },
}));

const ann: AdminUser = {
  id: "user-1",
  email: "ann@example.com",
  fullName: "Ann Admin",
  role: "org_admin",
  status: "active",
  lastLoginAt: null,
  createdAt: "2026-01-02T00:00:00Z",
};

beforeEach(() => {
  vi.useRealTimers();
  adminAccess = { allowed: true, canWrite: true, error: null, isLoading: false };
  usersQuery.mockClear();
  result = { data: { data: [ann], pagination: { hasMore: false, total: 1 } }, isLoading: false, error: null };
});

test("lists users with role, status, activity and a usage link", () => {
  render(<AdminUsersPage />);
  const row = screen.getByText("Ann Admin").closest("tr")!;
  expect(within(row).getByText("ann@example.com")).toBeInTheDocument();
  expect(within(row).getByText("Administrator")).toBeInTheDocument();
  expect(within(row).getByText("Active")).toBeInTheDocument();
  expect(within(row).getByText("Never")).toBeInTheDocument();
  expect(within(row).getByRole("link", { name: "Usage of Ann Admin" })).toHaveAttribute(
    "href",
    "/admin/analytics?userId=user-1"
  );
  expect(screen.getByText("1 user")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Users" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("link", { name: "Groups" })).toHaveAttribute("href", "/admin/groups");
});

test("searches after typing pauses and restarts from the first page", () => {
  vi.useFakeTimers();
  result = { data: { data: [ann], pagination: { hasMore: true, total: 120 } }, isLoading: false, error: null };
  render(<AdminUsersPage />);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(usersQuery).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50, search: "" }));

  fireEvent.change(screen.getByPlaceholderText("Search by name or email"), { target: { value: " ann " } });
  act(() => {
    vi.advanceTimersByTime(300);
  });
  expect(usersQuery).toHaveBeenLastCalledWith({ limit: 50, offset: 0, search: "ann" });
});

test("explains an empty directory and query failures", () => {
  result = { data: { data: [], pagination: { hasMore: false, total: 0 } }, isLoading: false, error: null };
  const { rerender } = render(<AdminUsersPage />);
  expect(screen.getByText("No users yet")).toBeInTheDocument();

  result = { data: undefined, isLoading: false, error: new Error("directory down") };
  rerender(<AdminUsersPage />);
  expect(screen.getByText("directory down")).toBeInTheDocument();
});

test("denies non-administrators", () => {
  adminAccess = { allowed: false, canWrite: false, error: "Administrators only", isLoading: false };
  render(<AdminUsersPage />);
  expect(screen.getByText("Administrators only")).toBeInTheDocument();
  expect(usersQuery).not.toHaveBeenCalled();
});
