import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";

import { useAdminUsage, useAdminUsers } from "@/hooks/useAdminAnalytics";

const getAdminUsage = vi.fn();
const getAdminUsers = vi.fn();

vi.mock("@/lib/api", () => ({
  getAdminUsage: (filters: unknown) => getAdminUsage(filters),
  getAdminUsers: (filters: unknown) => getAdminUsers(filters),
}));

let client: QueryClient;
function Wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  getAdminUsage.mockReset();
  getAdminUsers.mockReset();
  client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
});

test("uses normalized stable analytics and user query keys", async () => {
  getAdminUsage.mockResolvedValue({ data: { daily: [] }, meta: {} });
  getAdminUsers.mockResolvedValue({ data: [], meta: {}, pagination: {} });
  const usageFilters = {
    from: "2026-09-01",
    to: "2026-09-30",
    userId: undefined,
    model: "",
  };
  const userFilters = {
    limit: 50,
    offset: 0,
    search: undefined,
    status: undefined,
  };

  const { result } = renderHook(
    () => ({
      usage: useAdminUsage(usageFilters),
      users: useAdminUsers(userFilters),
    }),
    { wrapper: Wrapper }
  );

  await waitFor(() => expect(result.current.usage.isSuccess).toBe(true));
  await waitFor(() => expect(result.current.users.isSuccess).toBe(true));
  expect(getAdminUsage).toHaveBeenCalledWith({
    from: "2026-09-01",
    to: "2026-09-30",
  });
  expect(getAdminUsers).toHaveBeenCalledWith({ limit: 50, offset: 0 });
  expect(
    client.getQueryState([
      "admin-usage",
      { from: "2026-09-01", to: "2026-09-30" },
    ])
  ).toBeDefined();
  expect(
    client.getQueryState(["admin-users", { limit: 50, offset: 0 }])
  ).toBeDefined();
});
