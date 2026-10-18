import { renderHook } from "@testing-library/react";
import { vi } from "vitest";
import { useAdminAccess } from "@/hooks/useAdminAccess";

let authState: {
  isLoading: boolean;
  user: { role: string } | null;
} = { isLoading: true, user: null };

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => authState,
}));

beforeEach(() => {
  authState = { isLoading: true, user: null };
});

test("keeps access unresolved while authentication is loading", () => {
  const { result } = renderHook(() => useAdminAccess());
  expect(result.current).toMatchObject({ allowed: false, canWrite: false, isLoading: true });
});

test("allows only organization administrators", () => {
  authState = { isLoading: false, user: { role: "org_admin" } };
  const { result } = renderHook(() => useAdminAccess());
  expect(result.current).toMatchObject({ allowed: true, canWrite: true, error: null });
});

test("denies an ordinary member without probing legacy permission endpoints", () => {
  authState = { isLoading: false, user: { role: "member" } };
  const { result } = renderHook(() => useAdminAccess());
  expect(result.current).toMatchObject({
    allowed: false,
    canWrite: false,
    error: "Access denied. The organization administrator role is required.",
  });
});
