import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { useWorkflowDiagnostic } from "@/hooks/useWorkflows";

const load = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ getWorkflowDiagnostic: load }));

test("a missing diagnostic run is reported after one explicit request", async () => {
  load.mockRejectedValue(
    Object.assign(new Error("Run not found"), {
      response: new Response(null, { status: 404 }),
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retryDelay: 0 } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, unmount } = renderHook(
    () => useWorkflowDiagnostic("missing-run"),
    { wrapper },
  );
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(load).toHaveBeenCalledTimes(1);
  unmount();
  client.clear();
});
