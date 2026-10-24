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
const HELLO = "Hello from the deterministic Kubernetes gateway.";
type Run = { id: string; conversationId: string; status: string; errorCode: string | null;
  nodes: { phaseKey: string; status: string; errorCode: string | null;
    attemptCount: number; startedAt: string | null }[] };
type Fixture = { marker: string; member_id: string; source_workflow_id: string; source_agent_ids: { v1: string } };

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

// Only real API setup/readback, never browser auth/React/SSE state injection.
async function api<T>(page: Page, base: string, endpoint: string, method = "GET", body?: unknown, status = 200): Promise<T> {
  const result = await page.evaluate(async ({ url, method, body }) => {
    const response = await fetch(url, { method, headers: { Authorization: `Bearer ${window.keycloak?.token}`,
      "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const payload = await response.json();
    const id = payload.meta?.requestId;
    return { status: response.status, data: payload.data,
      requestId: typeof id === "string" && /^[0-9a-f-]{36}$/i.test(id) ? id : null };
  }, { url: `${base}${endpoint}`, method, body });
  expect(result.status, `${method} ${endpoint} requestId=${result.requestId}`).toBe(status);
  return result.data as T;
}

test.use({ trace: "off", video: "off", screenshot: "off" });
test.skip(process.env.E2E_WORKFLOW_TEMPLATES_UI !== "true" || process.env.WORKFLOW_ACCEPTANCE_KIND !== "hitl",
  "requires exclusive workflow-hitl launcher and 65s test runner");

test("real HITL reject, deadline heartbeats and exact-run cancellation", async ({ browser }) => {
  test.setTimeout(360_000);
  expect(MARKER).toMatch(/^workflow-templates-ui-e2e-[a-f0-9]{16}$/);
  const fixture = JSON.parse(readFileSync(STATE, "utf8")) as Fixture;
  expect(fixture.marker).toBe(MARKER);
  const adminContext = await browser.newContext();
  const memberContext = await browser.newContext();
  try {
    const admin = await adminContext.newPage();
    const member = await memberContext.newPage();
    await login(admin, true);
    await login(member, false);
    const definition = await api<{ version: number }>(admin, ADMIN_API, `/v1/admin/workflows/${fixture.source_workflow_id}`);
    await api(admin, ADMIN_API, `/v1/admin/workflows/${fixture.source_workflow_id}`, "PUT", {
      version: definition.version, name: `${MARKER}-source`, description: "Owned HITL acceptance",
      supervisorAgentDefinitionId: fixture.source_agent_ids.v1, autoRoutable: false, maxParallelism: 1,
      nodes: [{ nodeKey: "alpha", displayName: "alpha", agentDefinitionId: fixture.source_agent_ids.v1,
        instruction: "MULTI_AGENT_NODE:alpha", requiresConfirmation: true }], edges: [],
    });
    for (const [resourceType, resourceId] of [["workflow", fixture.source_workflow_id], ["agent_definition", fixture.source_agent_ids.v1], ["model", MODEL]]) {
      await api(admin, ADMIN_API, "/v1/admin/authorization/grants", "POST", {
        resourceType, resourceId, principalType: "user", principalId: fixture.member_id, permission: "read",
      }, 201);
    }
    await member.reload();
    await member.getByRole("button", { name: "Choose model" }).click();
    await member.getByRole("option", { name: new RegExp(`^${MODEL}(?:\\s|$)`) }).click();
    const created = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/conversations` && r.request().method() === "POST");
    const seed = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream` && r.request().method() === "POST");
    await member.getByLabel("Message").fill(`${MARKER}-run-v1`);
    await member.getByLabel("Send").click();
    const creation = await created;
    expect(creation.status()).toBe(200);
    const conversationId = (await creation.json()).data.id as string;
    execFileSync("python3", [path.join(ROOT, "scripts/workflow_templates_ui_e2e.py"), "record", "--state", STATE,
      "--marker", MARKER, "--kind", "conversation", "--id", conversationId], { cwd: ROOT, env: process.env, stdio: "inherit" });
    expect((await seed).status()).toBe(200);
    await expect(member.getByTestId("assistant-content").last()).toHaveText(HELLO, { timeout: 120_000 });
    expect(await (await seed).text()).toMatch(/^event: done\r?$/m);
    const previous: { id: string; status: string; errorCode: string | null }[] = [];
    const persisted = [HELLO];
    async function checkHistory() {
      const history = await api<{ messages: { data: { role: string; content: string }[] } }>(member, BUSINESS_API, `/v1/conversations/${conversationId}`);
      expect(history.messages.data.filter(m => m.role === "assistant").map(m => m.content)).toEqual(persisted);
    }

    for (const mode of ["reject", "timeout", "cancel"] as const) {
      await member.getByRole("button", { name: "Choose workflow" }).click();
      await member.getByRole("option", { name: new RegExp(`^${MARKER}-source(?:\\s|$)`) }).click();
      const sent = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream` && r.request().method() === "POST");
      await member.getByLabel("Message").fill(`HITL_${mode}`);
      const sentAt = performance.now();
      await member.getByLabel("Send").click();
      const response = await sent;
      expect(response.status()).toBe(200);
      expect(response.request().postDataJSON().workflowId).toBe(fixture.source_workflow_id);
      const progress = member.getByLabel("Workflow progress").last();
      await expect(progress.getByRole("button", { name: "alpha: reject", exact: true })).toBeVisible({ timeout: 90_000 });
      const waitingAt = performance.now();
      let runId = "";
      if (mode === "reject") {
        const decision = member.waitForResponse(r => /\/workflow-runs\/[^/]+\/nodes\/alpha\/decision$/.test(r.url()) && r.request().method() === "POST");
        await progress.getByRole("button", { name: "alpha: reject", exact: true }).click();
        const accepted = await decision;
        expect(accepted.status()).toBe(202);
        runId = accepted.url().match(/workflow-runs\/([^/]+)/)![1];
        // Chromium need not expose a streamed Fetch request body. Check the
        // server acknowledgement and later the persisted human_rejected result.
        expect((await accepted.json()).data).toEqual({ runId, nodeKey: "alpha", accepted: true });
      } else if (mode === "cancel") {
        const cancel = member.waitForResponse(r => /\/workflow-runs\/[^/]+\/cancel$/.test(r.url()) && r.request().method() === "POST");
        await member.getByLabel("Stop generating").click();
        const accepted = await cancel;
        expect(accepted.status()).toBe(202);
        runId = accepted.url().match(/workflow-runs\/([^/]+)/)![1];
      }
      await expect(progress).toContainText(mode === "cancel" ? "Workflow cancelled" : "Workflow finished with an error", { timeout: 100_000 });
      if (mode !== "cancel") {
        await expect(progress).toContainText(mode === "reject" ? "The step was rejected by a user" : "The confirmation timed out");
        const stream = await response.text();
        expect(stream).toMatch(/^event: done\r?$/m);
        const events = stream.split(/\r?\n\r?\n/).flatMap(block => {
          const type = block.match(/^event: (.+)$/m)?.[1]?.trim();
          const raw = block.match(/^data: (.+)$/m)?.[1];
          return type && raw ? [{ type, data: JSON.parse(raw) }] : [];
        });
        const nodes = events.filter(e => e.type === "workflow-node-status" && e.data.nodeKey === "alpha");
        expect(nodes.some(e => ["running", "completed"].includes(e.data.status))).toBe(false);
        expect(nodes.at(-1)?.data.status).toBe("failed");
        const terminal = events.filter(e => e.type === "workflow-run-status").at(-1)?.data;
        expect(terminal.status).toBe("failed");
        if (runId) expect(terminal.workflowRunId).toBe(runId);
        runId = terminal.workflowRunId;
        expect(events.some(e => e.type === "workflow-run-status" && e.data.status === "synthesizing")).toBe(false);
        expect(stream).not.toContain("MULTI_AGENT_NODE_RESULT:");
        expect(stream).not.toContain("MULTI_AGENT_FINAL:");
        if (mode === "timeout") {
          // Start before submission so delayed rendering cannot shorten this
          // measurement; repeated server heartbeats independently prove waiting.
          expect(performance.now() - sentAt).toBeGreaterThan(65_000);
          expect(nodes.filter(e => e.data.status === "waiting_human").length).toBeGreaterThanOrEqual(3);
        }
      }
      expect(runId).toMatch(/^[0-9a-f-]{36}$/i);
      expect(previous.map(r => r.id)).not.toContain(runId);
      const wanted = mode === "cancel" ? "cancelled" : "failed";
      // Cancel API commits the run before the signalled worker persists nodes.
      // Wait for both, not just the API's early terminal run acknowledgement.
      await expect.poll(async () => {
        const current = await api<Run>(member, BUSINESS_API, `/v1/workflow-runs/${runId}`);
        return { run: current.status, alpha: current.nodes.find(n => n.phaseKey === "alpha")?.status };
      }, { timeout: 30_000 }).toEqual({ run: wanted, alpha: wanted });
      const durable = await api<Run>(member, BUSINESS_API, `/v1/workflow-runs/${runId}`);
      expect(durable.conversationId).toBe(conversationId);
      // All phase rows are created with the run; presence is not execution.
      expect(durable.nodes.find(n => n.phaseKey === "supervisor_synthesize"))
        .toMatchObject({ attemptCount: 0, startedAt: null });
      const alpha = durable.nodes.find(n => n.phaseKey === "alpha");
      expect(alpha?.status).toBe(wanted);
      if (mode !== "cancel") expect(durable.errorCode).toBe(mode === "reject" ? "human_rejected" : "confirmation_timeout");
      await checkHistory();
      previous.push({ id: runId, status: durable.status, errorCode: durable.errorCode });
      await expect(member.getByRole("button", { name: "Choose workflow" })).toContainText("Plain chat");
      if (mode === "cancel") {
        // A later ordinary turn must survive exact-run cancellation. Then cross
        // the cancelled human deadline with real time to reject late answers.
        const next = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream` && r.request().method() === "POST");
        await member.getByLabel("Message").fill("HITL_RECOVERY");
        await member.getByLabel("Send").click();
        const nextResponse = await next;
        expect(nextResponse.status()).toBe(200);
        expect(nextResponse.request().postDataJSON().workflowId).toBeUndefined();
        await expect(member.getByTestId("assistant-content").last()).toHaveText(HELLO, { timeout: 90_000 });
        expect(await nextResponse.text()).toMatch(/^event: done\r?$/m);
        persisted.push(HELLO);
        await expect.poll(() => performance.now() - waitingAt, { timeout: 80_000, intervals: [1000] }).toBeGreaterThan(65_000);
        await checkHistory();
        for (const old of previous) {
          const current = await api<Run>(member, BUSINESS_API, `/v1/workflow-runs/${old.id}`);
          expect({ id: current.id, status: current.status, errorCode: current.errorCode }).toEqual(old);
        }
      }
    }
  } finally {
    await Promise.allSettled([adminContext.close(), memberContext.close()]);
    // The wrapper closes exact-owned Temporal trees before deleting runners/DB.
  }
});
