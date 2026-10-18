import { APIRequestContext, expect, test } from "@playwright/test";
import { login } from "./helpers/auth";

/**
 * /admin/workflows' canvas editor (WorkflowCanvas), real end-to-end against
 * the real admin backend (:8001) and Keycloak — no LLM Gateway involved.
 * Covers what the cross-app business-ui/e2e/workflow*.real.spec.ts specs
 * don't: canvas-only mechanics (add node, drag to reposition, connect two
 * handles, edit an edge's condition) in isolation, without a real chat run.
 */
const KEYCLOAK_URL = process.env.KEYCLOAK_URL || "http://localhost:8080";
const ADMIN_URL = process.env.ADMIN_BASE_URL || "http://localhost:8001";
const MODEL = process.env.E2E_WORKFLOW_CANVAS_MODEL || "gpt-4o-mini";

async function adminToken(request: APIRequestContext): Promise<string> {
  const response = await request.post(
    `${KEYCLOAK_URL}/realms/aurum-ai/protocol/openid-connect/token`,
    {
      form: {
        grant_type: "password",
        client_id: "admin-ui",
        username: "admin",
        password: "admin",
      },
    }
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()).access_token as string;
}

test.describe("Admin: visual workflow editor", () => {
  test("two nodes on the canvas: drag, connect, condition, save", async ({
    page,
    request,
  }) => {
    const token = await adminToken(request);
    const marker = `workflow-canvas-e2e-${Date.now()}`;

    const createdAgent = await request.post(`${ADMIN_URL}/v1/agent-definitions`, {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        name: marker,
        modelId: MODEL,
        systemPrompt: "test agent",
        temperature: 0.2,
        skillIds: [],
        knowledgeBaseIds: [],
      },
    });
    expect(createdAgent.status()).toBe(201);
    const agentId = ((await createdAgent.json()) as { data: { id: string } }).data.id;

    let workflowId: string | null = null;
    try {
      await login(page, "admin", "admin");
      await page.goto("/admin/workflows");
      await expect(page.getByRole("heading", { name: "Workflows", exact: true })).toBeVisible();

      await page.getByLabel("Workflow name").fill(marker);
      await page.getByLabel("Supervisor").selectOption(agentId);

      await page.getByLabel("Node 1 key").fill("alpha");
      await page.getByLabel("Node 1 name").fill("Alpha");
      await page.getByLabel("Node 1 agent").selectOption(agentId);
      await page.getByLabel("Node 1 instruction").fill("do alpha work");

      await page.getByRole("button", { name: "Add node" }).click();
      await page.getByLabel("Node 2 key").fill("beta");
      await page.getByLabel("Node 2 name").fill("Beta");
      await page.getByLabel("Node 2 agent").selectOption(agentId);
      await page.getByLabel("Node 2 instruction").fill("do beta work");

      // Drag Alpha away from its cascaded default spot so positionX/positionY
      // round-trips through save as a real, drag-produced value — a real
      // browser lays out and measures nodes for real, unlike jsdom in the
      // unit tests.
      const alphaWrapper = page.locator('.react-flow__node[data-id="alpha"]');
      const alphaBox = await alphaWrapper.boundingBox();
      if (!alphaBox) throw new Error("Alpha node did not render with a bounding box");
      const startX = alphaBox.x + alphaBox.width / 2;
      const startY = alphaBox.y + alphaBox.height / 2;
      await page.mouse.move(startX, startY);
      await page.mouse.down();
      await page.mouse.move(startX + 10, startY + 10, { steps: 2 });
      await page.mouse.move(startX + 60, startY + 150, { steps: 10 });
      await page.mouse.up();

      await page.getByLabel("Output handle — node Alpha").click();
      await page.getByLabel("Input handle — node Beta").click();
      await page
        .getByRole("combobox", { name: "Link condition alpha → beta", exact: true })
        .selectOption("on_failure");

      const created = page.waitForResponse(
        (response) =>
          response.url().endsWith("/v1/admin/workflows") &&
          response.request().method() === "POST"
      );
      await page.getByRole("button", { name: "Validate" }).click();
      await expect(page.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
      await page.getByRole("button", { name: "Save", exact: true }).click();
      const createdResponse = await created;
      expect(createdResponse.status()).toBe(201);
      const workflow = (await createdResponse.json()).data as {
        id: string;
        nodes: { nodeKey: string; positionX: number | null; positionY: number | null }[];
        edges: {
          sourceNodeKey: string;
          targetNodeKey: string;
          condition: string;
          isLoopBack: boolean;
          maxIterations: number | null;
        }[];
      };
      workflowId = workflow.id;
      expect(workflow.edges).toEqual([
        {
          sourceNodeKey: "alpha",
          targetNodeKey: "beta",
          condition: "on_failure",
          isLoopBack: false,
          maxIterations: null,
        },
      ]);
      const savedAlpha = workflow.nodes.find((node) => node.nodeKey === "alpha");
      // Alpha's cascaded default is (0, 0); the drag must have moved it.
      expect(savedAlpha?.positionX).not.toBe(0);
      expect(savedAlpha?.positionY).not.toBe(0);

      // Reopen for edit and confirm the graph survives a fresh mount.
      const row = page.locator("tr", { hasText: marker });
      await expect(row).toBeVisible();
      await row.getByRole("button", { name: "Edit", exact: true }).click();
      await expect(page.getByRole("heading", { name: "Edit workflow" })).toBeVisible();
      await expect(
        page.getByRole("combobox", { name: "Link condition alpha → beta", exact: true })
      ).toHaveValue("on_failure");
    } finally {
      if (workflowId) {
        await request.delete(`${ADMIN_URL}/v1/admin/workflows/${workflowId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      }
      await request.delete(`${ADMIN_URL}/v1/agent-definitions/${agentId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    }
  });

  test("self-loop through the node inspector: add, iterations, save", async ({
    page,
    request,
  }) => {
    const token = await adminToken(request);
    const marker = `workflow-loop-e2e-${Date.now()}`;

    const createdAgent = await request.post(`${ADMIN_URL}/v1/agent-definitions`, {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        name: marker,
        modelId: MODEL,
        systemPrompt: "test agent",
        temperature: 0.2,
        skillIds: [],
        knowledgeBaseIds: [],
      },
    });
    expect(createdAgent.status()).toBe(201);
    const agentId = ((await createdAgent.json()) as { data: { id: string } }).data.id;

    let workflowId: string | null = null;
    try {
      await login(page, "admin", "admin");
      await page.goto("/admin/workflows");
      await expect(page.getByRole("heading", { name: "Workflows", exact: true })).toBeVisible();

      await page.getByLabel("Workflow name").fill(marker);
      await page.getByLabel("Supervisor").selectOption(agentId);

      await page.getByLabel("Node 1 key").fill("loopy");
      await page.getByLabel("Node 1 name").fill("Loopy");
      await page.getByLabel("Node 1 agent").selectOption(agentId);
      await page.getByLabel("Node 1 instruction").fill("do loopy work");

      await page.getByRole("button", { name: "Add a loop on this node 1" }).click();
      await expect(
        page.getByRole("button", { name: "Add a loop on this node 1" })
      ).toHaveCount(0);

      await page.getByLabel("Loop parameters loopy → loopy").click();
      await page.getByLabel("Loop iterations loopy → loopy").fill("5");

      const created = page.waitForResponse(
        (response) =>
          response.url().endsWith("/v1/admin/workflows") &&
          response.request().method() === "POST"
      );
      await page.getByRole("button", { name: "Validate" }).click();
      await expect(page.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
      await page.getByRole("button", { name: "Save", exact: true }).click();
      const createdResponse = await created;
      expect(createdResponse.status()).toBe(201);
      const workflow = (await createdResponse.json()).data as {
        id: string;
        edges: {
          sourceNodeKey: string;
          targetNodeKey: string;
          condition: string;
          isLoopBack: boolean;
          maxIterations: number | null;
        }[];
      };
      workflowId = workflow.id;
      expect(workflow.edges).toEqual([
        {
          sourceNodeKey: "loopy",
          targetNodeKey: "loopy",
          condition: "on_failure",
          isLoopBack: true,
          maxIterations: 5,
        },
      ]);

      // Reopen for edit and confirm the loop survives a fresh mount.
      const row = page.locator("tr", { hasText: marker });
      await expect(row).toBeVisible();
      await row.getByRole("button", { name: "Edit", exact: true }).click();
      await expect(page.getByRole("heading", { name: "Edit workflow" })).toBeVisible();
      await page.getByLabel("Loop parameters loopy → loopy").click();
      await expect(page.getByLabel("Loop iterations loopy → loopy")).toHaveValue("5");
    } finally {
      if (workflowId) {
        await request.delete(`${ADMIN_URL}/v1/admin/workflows/${workflowId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      }
      await request.delete(`${ADMIN_URL}/v1/agent-definitions/${agentId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    }
  });
});
