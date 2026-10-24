import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";
import { expectSelectedModel } from "./helpers/chat";

/**
 * Full real round trip for a skill call driven through the actual browser:
 * login -> real LLM emits a <tool_call> -> real tool-worker -> real MCP
 * server (mcp-everything) -> result rendered in the UI. Same opt-in gate as
 * chat.real-llm.spec.ts (E2E_USE_REAL_LLM) since this needs a real LLM Gateway
 * to reliably emit a well-formed tool call, plus:
 *   docker compose --profile mcp up -d mcp-everything
 *
 * Skill is registered by seeding Postgres directly (docker exec), not via
 * POST /v1/skills/ — the default e2e user (user/user) only has
 * chat:write/files:read, not admin:write. Mirrors smoke-test-mcp.sh's setup,
 * through the browser instead of curl.
 */
const ENABLED = process.env.E2E_USE_REAL_LLM === "true";
const MODEL = process.env.E2E_LLM_MODEL || "";
if (ENABLED && !MODEL) {
  throw new Error("Set E2E_LLM_MODEL when E2E_USE_REAL_LLM=true.");
}
const POSTGRES_CONTAINER = process.env.E2E_POSTGRES_CONTAINER || "aurum-ai-postgres";

function orgIdFromToken(token: string): string {
  const payload = token.split(".")[1];
  const padded = payload + "=".repeat((4 - (payload.length % 4)) % 4);
  const json = Buffer.from(padded, "base64url").toString("utf-8");
  return JSON.parse(json).org_id as string;
}

function psql(sql: string): void {
  execFileSync("docker", [
    "exec",
    "-i",
    POSTGRES_CONTAINER,
    "psql",
    "-U",
    "postgres",
    "-d",
    "aurum_ai",
    "-v",
    "ON_ERROR_STOP=1",
    "-c",
    sql,
  ]);
}

test.describe("Chat: real MCP skill call", () => {
  test.skip(!ENABLED, "E2E_USE_REAL_LLM is not set; test skipped");

  // Computed inside beforeAll (worker-process time), not at module scope —
  // a value baked into the test title itself would differ between
  // Playwright's list phase and run phase (separate module evaluations) and
  // the worker would fail with "Test not found in the worker process".
  let skillName = "";
  let skillId = "";
  test.beforeAll(() => {
    skillName = `e2e-realskill-sum-${Date.now()}`;
    // Generated here (not gen_random_uuid() in SQL) so the test knows the
    // real id upfront: the LLM's <tool_call> carries the skill's id, not its
    // display name (see conversation.py's _build_skills_system_message /
    // AGENT_SYSTEM_PROMPT — "skill_id": "<id>") — that's what ends up in the
    // ToolCallRow's aria-label, so that's what must be matched below.
    skillId = randomUUID();
  });

  test.afterAll(() => {
    psql(
      `DELETE FROM access_grants WHERE resource_type = 'skill' AND resource_id = '${skillId}'; ` +
        `DELETE FROM skills WHERE id = '${skillId}';`
    );
  });

  test(`real MCP skill call through model ${MODEL}`, async ({ page }) => {
    // Default Playwright per-test timeout (30s) can be tight for a real LLM
    // round trip + tool call.
    test.setTimeout(120_000);

    await login(page);

    const token = await page.evaluate(() => window.keycloak?.token);
    if (!token) throw new Error("window.keycloak.token is unavailable after login");
    const orgId = orgIdFromToken(token);

    psql(
      `INSERT INTO skills (id, org_id, name, description, type, mcp_transport, mcp_endpoint, activity_name, input_schema, created_at, updated_at) VALUES ` +
        `('${skillId}', '${orgId}', '${skillName}', 'Returns the sum of two numbers', 'mcp', 'sse', 'http://mcp-everything:3001/sse', 'get-sum', ` +
        `'{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}', now(), now()); ` +
        `INSERT INTO access_grants (id, org_id, resource_type, resource_id, principal_type, principal_id, permission, created_via) ` +
        `VALUES ('${randomUUID()}', '${orgId}', 'skill', '${skillId}', 'organization', '${orgId}', 'read', 'provisioning');`
    );

    await page.getByRole("button", { name: "Choose model" }).click();
    await page.getByRole("option", { name: MODEL, exact: true }).click();

    const composer = page.getByLabel("Message");
    await composer.fill(
      "What is 17 plus 25? Use the available tool to compute it, then answer with just the number."
    );
    await expectSelectedModel(page, MODEL);
    await page.getByLabel("Send").click();

    // aria-label lives on a plain <div> (see ToolCallRow.tsx) — getByLabel()
    // targets labelled form controls, so an attribute locator is used here.
    const toolCall = page.locator(`[aria-label="${skillId}: Done"]`);
    await expect(toolCall.first()).toBeVisible({ timeout: 90_000 });
    // A completed status means a tool-result event arrived, not that MCP itself
    // succeeded. A failed activity can still be followed by an LLM answer
    // containing the expected number, so require the real MCP success flag
    // from the tool-result payload as well.
    await expect(page.getByLabel("Send")).toBeVisible({ timeout: 90_000 });
    for (const card of await toolCall.all()) {
      await expect(card).toContainText('"isError":false');
    }
    // Scoped to the final answer's own testid, not exact-matched — "42"
    // also appears as a substring of the tool-call detail line rendered by
    // ToolCallRow (also a <p>, so a bare page-wide getByText("42") would
    // hit strict-mode violations), and not every model phrases the answer
    // as the bare number.
    await expect(
      page.getByTestId("assistant-content").last()
    ).toContainText(/\b42\b/);
  });
});
