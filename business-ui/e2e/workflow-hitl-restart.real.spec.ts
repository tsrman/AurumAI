import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

// Proves the gap ROADMAP.md §5 flagged: a waiting_human node must survive the
// conversation-runner container being stopped and restarted while it waits,
// with a decision signalled *while the worker is down* still processed
// correctly once it comes back (Temporal durable execution/replay). Runs
// against the plain runner image with the production 1800s confirmation
// deadline — the shortened 65s test image used by workflow-hitl.real.spec.ts
// is unsuitable here: workflow.now() keeps advancing during the outage, and a
// restart cycle (up to 60s health-poll alone) could race a short deadline.
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

function fixtureAction(action: "stop-worker" | "start-worker", conversationId: string) {
  execFileSync("python3", [path.join(ROOT, "scripts/workflow_templates_ui_e2e.py"), action,
    "--state", STATE, "--marker", MARKER, "--conversation", conversationId],
    { cwd: ROOT, env: process.env, stdio: "inherit" });
}

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
test.skip(process.env.E2E_WORKFLOW_TEMPLATES_UI !== "true" || process.env.WORKFLOW_ACCEPTANCE_KIND !== "hitl-restart",
  "requires exclusive workflow-hitl-restart launcher and the production-timeout runner image");

test("real HITL survives a conversation-runner restart mid-wait", async ({ browser }) => {
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
      version: definition.version, name: `${MARKER}-source`, description: "Owned HITL restart acceptance",
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

    await member.getByRole("button", { name: "Choose workflow" }).click();
    await member.getByRole("option", { name: new RegExp(`^${MARKER}-source(?:\\s|$)`) }).click();
    const sent = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream` && r.request().method() === "POST");
    await member.getByLabel("Message").fill("HITL_RESTART");
    await member.getByLabel("Send").click();
    const sendResponse = await sent;
    expect(sendResponse.status()).toBe(200);
    expect(sendResponse.request().postDataJSON().workflowId).toBe(fixture.source_workflow_id);
    const progress = member.getByLabel("Workflow progress").last();
    await expect(progress.getByRole("button", { name: "alpha: reject", exact: true })).toBeVisible({ timeout: 90_000 });

    // The runner is now genuinely gone; only Temporal server + Postgres carry
    // the workflow's state until the same container comes back.
    fixtureAction("stop-worker", conversationId);

    // Signal *while the worker is down*: the decision endpoint only talks to
    // the Temporal server, so this must still succeed (202) with nobody
    // polling the task queue — the more rigorous half of the durability claim.
    const decision = member.waitForResponse(r => /\/workflow-runs\/[^/]+\/nodes\/alpha\/decision$/.test(r.url()) && r.request().method() === "POST");
    await progress.getByRole("button", { name: "alpha: reject", exact: true }).click();
    const accepted = await decision;
    expect(accepted.status()).toBe(202);
    const runId = accepted.url().match(/workflow-runs\/([^/]+)/)![1];
    expect((await accepted.json()).data).toEqual({ runId, nodeKey: "alpha", accepted: true });

    // Same container restarted, not a fresh one — proves the SAME worker
    // process resumes polling the same task queue (session-<conversation_id>)
    // rather than a re-bind/rehydration path masking the real question.
    fixtureAction("start-worker", conversationId);

    // The browser's original /v1/chat/stream connection may or may not still
    // be alive (bounded by EVENT_SUBSCRIBE_TIMEOUT=120s, and the restart cycle
    // above can consume much of that) — deliberately not asserted on. The
    // durable REST endpoint is the source of truth, independent of any one
    // browser connection's lifetime.
    await expect.poll(async () => {
      const current = await api<Run>(member, BUSINESS_API, `/v1/workflow-runs/${runId}`);
      return { run: current.status, alpha: current.nodes.find(n => n.phaseKey === "alpha")?.status };
    }, { timeout: 90_000 }).toEqual({ run: "failed", alpha: "failed" });

    const durable = await api<Run>(member, BUSINESS_API, `/v1/workflow-runs/${runId}`);
    expect(durable.conversationId).toBe(conversationId);
    expect(durable.errorCode).toBe("human_rejected");
    // All phase rows are created with the run; presence is not execution.
    expect(durable.nodes.find(n => n.phaseKey === "supervisor_synthesize"))
      .toMatchObject({ attemptCount: 0, startedAt: null });
    const alpha = durable.nodes.find(n => n.phaseKey === "alpha");
    expect(alpha?.status).toBe("failed");
    expect(alpha?.errorCode).toBe("human_rejected");

    // Only the seed HELLO turn is durably persisted — the restarted turn never
    // produced assistant content (rejected before synthesis), and nothing
    // leaked or duplicated across the restart.
    const history = await api<{ messages: { data: { role: string; content: string }[] } }>(member, BUSINESS_API, `/v1/conversations/${conversationId}`);
    expect(history.messages.data.filter(m => m.role === "assistant").map(m => m.content)).toEqual([HELLO]);
    await expect(member.getByRole("button", { name: "Choose workflow" })).toContainText("Plain chat");
  } finally {
    await Promise.allSettled([adminContext.close(), memberContext.close()]);
    // The wrapper closes exact-owned Temporal trees before deleting runners/DB.
  }
});
