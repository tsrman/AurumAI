import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";

import {
  useAdminModels,
  useUpdateOrganizationModelSettings,
} from "@/hooks/useAdminModels";

const getAdminModels = vi.fn();
const putOrganizationModelSettings = vi.fn();

vi.mock("@/lib/api", () => ({
  getAdminModels: () => getAdminModels(),
  putOrganizationModelSettings: (settings: {
    defaultModel: string | null;
    defaultTemperature: number | null;
  }) => putOrganizationModelSettings(settings),
}));

let client: QueryClient;

function Wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  getAdminModels.mockReset();
  putOrganizationModelSettings.mockReset();
  client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
});

test("loads admin models with the stable query key", async () => {
  getAdminModels.mockResolvedValue({
    data: {
      gatewayStatus: "available",
      defaultModel: null,
      defaultAvailable: false,
      models: [],
    },
    meta: {},
  });
  const { result } = renderHook(() => useAdminModels(), { wrapper: Wrapper });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(client.getQueryData(["admin-models"])).toEqual(result.current.data);
});

test("updates nullable default and invalidates admin and user model catalogs", async () => {
  putOrganizationModelSettings.mockResolvedValue({
    data: { defaultModel: null, defaultTemperature: null },
    meta: {},
  });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const { result } = renderHook(
    () => useUpdateOrganizationModelSettings(),
    { wrapper: Wrapper }
  );

  await act(async () => {
    await result.current.mutateAsync({
      defaultModel: null,
      defaultTemperature: null,
    });
  });

  expect(putOrganizationModelSettings).toHaveBeenCalledWith({
    defaultModel: null,
    defaultTemperature: null,
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin-models"] });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["models"] });
});
