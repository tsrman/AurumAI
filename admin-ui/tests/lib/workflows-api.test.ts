import { vi } from "vitest";

import {
  archiveWorkflow,
  createWorkflow,
  getAdminWorkflows,
  getWorkflowDiagnostic,
  replaceWorkflow,
  validateWorkflow,
} from "@/lib/api";
import type { WorkflowDraft } from "@/lib/api";
import { createTranslator } from "next-intl";
import { getApiErrorMessage } from "@/lib/errors";
import en from "../../messages/en.json";

const draft: WorkflowDraft = {
  name: "Contract review",
  supervisorAgentDefinitionId: "supervisor",
  maxParallelism: 2,
  nodes: [
    {
      nodeKey: "legal",
      displayName: "Legal",
      agentDefinitionId: "agent-legal",
      instruction: "Review clauses",
    },
  ],
  edges: [],
};

beforeEach(() => {
  vi.stubGlobal("window", { keycloak: { token: "admin-token" } });
});

afterEach(() => vi.unstubAllGlobals());

test("workflow API uses camelCase bodies and retry-safe PUT replacement", async () => {
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      requests.push(request.clone());
      if (request.method === "DELETE") return new Response(null, { status: 204 });
      return new Response(JSON.stringify({ data: {}, meta: {} }), {
        headers: { "Content-Type": "application/json" },
      });
    })
  );

  await getAdminWorkflows(true);
  await createWorkflow(draft);
  await replaceWorkflow("workflow-1", 7, draft);
  await validateWorkflow(draft);
  await getWorkflowDiagnostic("run-1");
  await archiveWorkflow("workflow-1");

  expect(new URL(requests[0].url).searchParams.get("includeArchived")).toBe("true");
  expect(new URL(requests[1].url).pathname).toBe("/v1/admin/workflows");
  await expect(requests[1].json()).resolves.toEqual(draft);
  expect(requests[2].method).toBe("PUT");
  await expect(requests[2].json()).resolves.toEqual({ ...draft, version: 7 });
  expect(new URL(requests[3].url).pathname).toBe("/v1/admin/workflows/validate");
  expect(new URL(requests[4].url).pathname).toBe("/v1/admin/workflow-runs/run-1");
  expect(requests[5].method).toBe("DELETE");
});

// business-backend's generic HTTPException handler (middleware/error_handler.py)
// wraps every error body as {error: {code, message}} — message IS the
// WorkflowGraphError's {code, nodeKeys} object, never a top-level `detail`
// field. Before this fix, every WorkflowGraphError (cycle, duplicate_edge,
// the loop-specific codes, ...) surfaced to the admin as a generic
// "Request failed with status code 422", with the real detail silently lost.
test("validateWorkflow surfaces a WorkflowGraphError's {code, nodeKeys} as a friendly message", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { code: "FORBIDDEN", message: { code: "invalid_loop_edge", nodeKeys: ["b"] } },
          meta: {},
        }),
        { status: 422, headers: { "Content-Type": "application/json" } }
      )
    )
  );

  const error = await validateWorkflow(draft).catch((cause: unknown) => cause);
  expect(error).toMatchObject({ messageKey: "workflowGraph.invalid_loop_edge", detail: "b" });
  const t = createTranslator({ locale: "en", messages: en, namespace: "errors" }) as unknown as Parameters<
    typeof getApiErrorMessage
  >[1];
  expect(getApiErrorMessage(error, t)).toBe(
    "A loop link must lead to a preceding node or to the node itself. (b)"
  );
});

test("replaceWorkflow surfaces a plain-string error message unchanged", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ error: { code: "FORBIDDEN", message: "Workflow version conflict" }, meta: {} }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      )
    )
  );

  await expect(replaceWorkflow("workflow-1", 7, draft)).rejects.toThrow("Workflow version conflict");
});
