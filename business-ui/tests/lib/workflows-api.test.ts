import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cancelWorkflowRun, decideWorkflowNode, listWorkflows } from "@/lib/workflows-api";

describe("workflows-api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("window", { keycloak: { token: "test-token" } });
  });

  afterEach(() => vi.unstubAllGlobals());

  test("returns readable active workflows", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: [
            {
              id: "workflow-1",
              name: "Research",
              description: "Parallel analysis",
              version: 3,
              maxParallelism: 2,
              status: "active",
            },
          ],
          meta: { requestId: "r1", timestamp: "2026-09-02T00:00:00Z" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );

    await expect(listWorkflows()).resolves.toEqual([
      expect.objectContaining({ id: "workflow-1", name: "Research" }),
    ]);
  });

  test("cancels one exact workflow run", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 202 }));

    await cancelWorkflowRun("run-1");

    const [request] = vi.mocked(fetch).mock.calls[0] as [Request];
    expect(request.method).toBe("POST");
    expect(request.url.endsWith("/workflow-runs/run-1/cancel")).toBe(true);
    expect(request.headers.get("Authorization")).toBe("Bearer test-token");
  });

  test("submits an approve/reject decision for one exact node", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 202 }));

    await decideWorkflowNode("run-1", "publish", "reject");

    const [request] = vi.mocked(fetch).mock.calls[0] as [Request];
    expect(request.method).toBe("POST");
    expect(request.url.endsWith("/workflow-runs/run-1/nodes/publish/decision")).toBe(true);
    expect(request.headers.get("content-type")).toBe("application/json");
  });
});
