import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { useAdminModels } from "@/hooks/useAdminModels";
import { useReconcileGrants } from "@/hooks/useAuthorization";

let readable = false;
vi.mock("@/lib/api", () => ({
  getAdminModels: async () => ({ data: {
    gatewayStatus: "available", defaultModel: null, defaultTemperature: null,
    defaultAvailable: false,
    models: [{ id: "test/model", organizationReadable: readable, isDefault: false }],
  } }),
  reconcileResourceGrants: async () => {
    readable = true;
    return { data: [] };
  },
}));

test("refreshes the default-model catalog after a model grant is applied", async () => {
  readable = false;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => ({ models: useAdminModels(), grants: useReconcileGrants() }), { wrapper });
  await waitFor(() => expect(result.current.models.isSuccess).toBe(true));
  expect(result.current.models.data?.data.models[0].organizationReadable).toBe(false);

  await act(async () => {
    await result.current.grants.mutateAsync({ resourceType: "model", resourceId: "test/model", grants: [
      { principalType: "organization", principalId: "org-1", permission: "read" },
    ] });
  });

  await waitFor(() => expect(result.current.models.data?.data.models[0].organizationReadable).toBe(true));
  client.clear();
});
