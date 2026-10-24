import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const ROOT = path.resolve(__dirname, "../..");
const STATE = process.env.E2E_WORKFLOW_TEMPLATES_UI_STATE || "";
const MARKER = process.env.E2E_WORKFLOW_TEMPLATES_UI_MARKER || "";
const ADMIN = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";
const BUSINESS = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const ADMIN_API = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const BUSINESS_API = process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
const MODEL = `${MARKER}-model`;
type Fixture = { marker: string; member_id: string; source_workflow_id: string; source_agent_ids: { v1: string } };
type Workflow = { id: string; version: number };
type Event = { type: string; data: { status?: string; nodeKey?: string; workflowRunId?: string; errorCode?: string } };
type Edge = { sourceNodeKey: string; targetNodeKey: string; condition: "on_success" | "on_failure" };
const edge = (sourceNodeKey: string, targetNodeKey: string, condition: Edge["condition"] = "on_success"): Edge => ({ sourceNodeKey, targetNodeKey, condition });

async function login(page: Page, admin: boolean) {
  const url = admin ? ADMIN : BUSINESS;
  await page.goto(`${url}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.locator("#username").fill(admin ? process.env.E2E_ADMIN_USERNAME || "admin" : process.env.E2E_MEMBER_USERNAME || "user");
  await page.locator("#password").fill(admin ? process.env.E2E_ADMIN_PASSWORD || "admin" : process.env.E2E_MEMBER_PASSWORD || "user");
  await page.locator("#kc-login").click();
  await page.waitForURL(`${url}${admin ? "/admin/users" : "/chat"}`);
  await expect.poll(() => page.evaluate(() => Boolean(window.keycloak?.token))).toBe(true);
}

// Actual administrative API setup, not UI-authoring acceptance. No JWT crosses
// the browser boundary; no write retries or injected query-cache state.
async function api<T>(page: Page, base: string, endpoint: string, method = "GET", body?: unknown, status = 200): Promise<T> {
  const result = await page.evaluate(async ({ url, method, body }) => {
    const response = await fetch(url, { method, headers: {
      Authorization: `Bearer ${window.keycloak?.token}`, "Content-Type": "application/json",
    }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const payload = await response.json();
    const id = payload.meta?.requestId;
    return { status: response.status, data: payload.data,
      requestId: typeof id === "string" && /^[0-9a-f-]{36}$/i.test(id) ? id : null };
  }, { url: `${base}${endpoint}`, method, body });
  expect(result.status, `${method} ${endpoint} requestId=${result.requestId}`).toBe(status);
  return result.data as T;
}

function events(stream: string): Event[] {
  return stream.split(/\r?\n\r?\n/).flatMap(block => {
    const type = block.match(/^event: (.+)$/m)?.[1]?.trim();
    const data = block.match(/^data: (.+)$/m)?.[1];
    return type && data ? [{ type, data: JSON.parse(data) }] : [];
  });
}

test.use({ trace: "off", video: "off", screenshot: "off" });
test.skip(process.env.E2E_WORKFLOW_TEMPLATES_UI !== "true" || process.env.WORKFLOW_ACCEPTANCE_KIND !== "branching",
  "requires exclusive workflow-branching launcher");

test("handled failure, AND-join/skip cascade and unhandled failure through real chat", async ({ browser }) => {
  test.setTimeout(360_000);
  expect(MARKER).toMatch(/^workflow-templates-ui-e2e-[a-f0-9]{16}$/);
  const state = JSON.parse(readFileSync(STATE, "utf8")) as Fixture;
  expect(state.marker).toBe(MARKER);
  const adminContext = await browser.newContext();
  const memberContext = await browser.newContext();
  try {
    const admin = await adminContext.newPage();
    const member = await memberContext.newPage();
    await login(admin, true);
    await login(member, false);
    let definition = await api<Workflow>(admin, ADMIN_API, `/v1/admin/workflows/${state.source_workflow_id}`);
    for (const [resourceType, resourceId] of [["workflow", definition.id], ["agent_definition", state.source_agent_ids.v1], ["model", MODEL]]) {
      await api(admin, ADMIN_API, "/v1/admin/authorization/grants", "POST", {
        resourceType, resourceId, principalType: "user", principalId: state.member_id, permission: "read",
      }, 201);
    }
    await member.reload();
    await member.getByRole("button", { name: "Choose model" }).click();
    await member.getByRole("option", { name: new RegExp(`^${MODEL}(?:\\s|$)`) }).click();
    // Fix the exact conversation title accepted by durable recovery before the
    // error-producing turns. Record its UUID immediately, even if seed fails.
    const created = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/conversations` && r.request().method() === "POST");
    const seedSent = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream` && r.request().method() === "POST");
    await member.getByLabel("Message").fill(`${MARKER}-run-v1`);
    await member.getByLabel("Send").click();
    const creation = await created;
    expect(creation.status()).toBe(200);
    const conversationId = (await creation.json()).data.id as string;
    execFileSync("python3", [path.join(ROOT, "scripts/workflow_templates_ui_e2e.py"), "record",
      "--state", STATE, "--marker", MARKER, "--kind", "conversation", "--id", conversationId],
    { cwd: ROOT, env: process.env, stdio: "inherit" });
    expect((await seedSent).status()).toBe(200);
    await expect(member.getByTestId("assistant-content").last()).toHaveText("Hello from the deterministic Kubernetes gateway.", { timeout: 120_000 });
    expect(await (await seedSent).text()).toMatch(/^event: done\r?$/m);
    const persisted = ["Hello from the deterministic Kubernetes gateway."];

    async function runCase(name: string, keys: string[], edges: Edge[], expected: Record<string, string>, answer?: string) {
      definition = await api<Workflow>(admin, ADMIN_API, `/v1/admin/workflows/${definition.id}`, "PUT", {
        version: definition.version, name: `${MARKER}-source`, description: "Owned branching acceptance",
        supervisorAgentDefinitionId: state.source_agent_ids.v1, autoRoutable: false, maxParallelism: 2, edges,
        nodes: keys.map(nodeKey => ({ nodeKey, displayName: nodeKey, agentDefinitionId: state.source_agent_ids.v1,
          requiresConfirmation: false, instruction: `${nodeKey === "alpha" ? "MULTI_AGENT_FAIL:alpha\n" : ""}MULTI_AGENT_NODE:${nodeKey}${nodeKey === "beta" ? "\nMULTI_AGENT_BARRIER:branching-beta" : ""}` })),
      });
      await member.getByRole("button", { name: "Choose workflow" }).click();
      await member.getByRole("option", { name: new RegExp(`^${MARKER}-source(?:\\s|$)`) }).click();
      const sent = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream` && r.request().method() === "POST");
      await member.getByLabel("Message").fill(name);
      await member.getByLabel("Send").click();
      const response = await sent;
      expect(response.status()).toBe(200);
      expect(response.request().postDataJSON().workflowId).toBe(definition.id);
      const progress = member.getByLabel("Workflow progress").last();
      await expect(progress).toContainText(answer ? "Workflow completed" : "Workflow finished with an error", { timeout: 120_000 });
      const stream = await response.text();
      const received = events(stream);
      expect(received.some(e => e.type === "done")).toBe(true);
      const nodeEvents = received.filter(e => e.type === "workflow-node-status");
      const finalStatuses = Object.fromEntries(nodeEvents.map(e => [e.data.nodeKey, e.data.status]));
      expect(finalStatuses).toEqual(expected);
      expect(received.filter(e => e.type === "workflow-run-status").at(-1)?.data.status).toBe(answer ? "completed" : "failed");
      const labels: Record<string, string> = { failed: "Error", skipped: "Skipped — condition not met", completed: "Done" };
      for (const [key, status] of Object.entries(expected)) {
        await expect(progress.getByRole("listitem").filter({ has: member.getByText(key, { exact: true }) })).toContainText(labels[status]);
        if (status === "skipped") expect(nodeEvents.some(e => e.data.nodeKey === key && e.data.status === "running")).toBe(false);
      }
      if (answer) {
        expect(received.some(e => e.type === "error")).toBe(false);
        await expect(member.getByTestId("assistant-content").last()).toHaveText(answer);
        persisted.push(answer);
        const joinStart = nodeEvents.findIndex(e => e.data.nodeKey === "zeta" && e.data.status === "running");
        for (const key of ["beta", "gamma"]) {
          const predecessorDone = nodeEvents.findIndex(e => e.data.nodeKey === key && e.data.status === "completed");
          expect(predecessorDone).toBeGreaterThanOrEqual(0);
          expect(joinStart).toBeGreaterThan(predecessorDone);
        }
      } else {
        expect(received.some(e => e.type === "workflow-run-status" && e.data.status === "synthesizing")).toBe(false);
        expect(stream).not.toContain("MULTI_AGENT_FINAL:");
        if (name === "BRANCHING_UNHANDLED") {
          // Unhandled failure aborts scheduling immediately. Pending beta is
          // never launched; unlike handled branches it has no skip transition.
          expect(nodeEvents.some(e => e.data.nodeKey === "beta")).toBe(false);
        }
      }
      expect(stream).not.toContain("Deterministic node failure");
      const history = await api<{ messages: { data: { role: string; content: string }[] } }>(member, BUSINESS_API, `/v1/conversations/${conversationId}`);
      expect(history.messages.data.filter(m => m.role === "assistant").map(m => m.content)).toEqual(persisted);
      await expect(member.getByRole("button", { name: "Choose workflow" })).toContainText("Plain chat");
    }

    await runCase("BRANCHING_HANDLED", ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"], [
      edge("alpha", "gamma", "on_failure"), edge("alpha", "delta"), edge("delta", "epsilon"),
      edge("beta", "zeta"), edge("gamma", "zeta"),
    ], { alpha: "failed", beta: "completed", gamma: "completed", delta: "skipped", epsilon: "skipped", zeta: "completed" }, "MULTI_AGENT_FINAL:beta,gamma,zeta");
    await runCase("BRANCHING_JOIN_BLOCKED", ["alpha", "beta", "gamma"], [
      edge("alpha", "beta"), edge("alpha", "gamma", "on_failure"), edge("beta", "gamma"),
    ], { alpha: "failed", beta: "skipped", gamma: "skipped" });
    await runCase("BRANCHING_UNHANDLED", ["alpha", "beta"], [edge("alpha", "beta")],
      { alpha: "failed" });
  } finally {
    await Promise.allSettled([adminContext.close(), memberContext.close()]);
    // Wrapper validates/terminates only exact-owned Temporal executions first.
  }
});
