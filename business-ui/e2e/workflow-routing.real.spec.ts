import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const ENABLED = process.env.E2E_WORKFLOW_TEMPLATES_UI === "true"
  && process.env.WORKFLOW_ACCEPTANCE_KIND === "routing";
const STATE = process.env.E2E_WORKFLOW_TEMPLATES_UI_STATE || "";
const MARKER = process.env.E2E_WORKFLOW_TEMPLATES_UI_MARKER || "";
const ADMIN = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";
const BUSINESS = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const ADMIN_API = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const BUSINESS_API = process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
const ROOT = path.resolve(__dirname, "../..");
const MODEL = `${MARKER}-model`;
type Fixture = { marker: string; member_id: string; source_workflow_id: string; source_agent_ids: { v1: string } };
type Workflow = { id: string; version: number };

function record(kind: "template" | "workflow" | "agent" | "conversation", id: string) {
  execFileSync("python3", [path.join(ROOT, "scripts/workflow_templates_ui_e2e.py"),
    "record", "--state", STATE, "--marker", MARKER, "--kind", kind, "--id", id],
  { cwd: ROOT, env: process.env, stdio: "inherit" });
}

async function login(page: Page, admin: boolean) {
  const url = admin ? ADMIN : BUSINESS;
  await page.goto(`${url}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.locator("#username").fill(admin ? process.env.E2E_ADMIN_USERNAME || "admin" : process.env.E2E_MEMBER_USERNAME || "user");
  await page.locator("#password").fill(admin ? process.env.E2E_ADMIN_PASSWORD || "admin" : process.env.E2E_MEMBER_PASSWORD || "user");
  await page.locator("#kc-login").click();
  await page.waitForURL(`${url}${admin ? "/admin/agents" : "/chat"}`);
  await expect.poll(() => page.evaluate(() => Boolean(window.keycloak?.token))).toBe(true);
}

// Real administrative API setup, not a UI-authoring/ACL acceptance. Token stays
// inside its authenticated browser. Only fixture metadata crosses the boundary.
async function api<T>(page: Page, base: string, endpoint: string, method = "GET", body?: unknown, status = 200): Promise<T> {
  const result = await page.evaluate(async ({ url, method, body }) => {
    const token = window.keycloak?.token;
    const response = await fetch(url, { method,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const payload = await response.json();
    let authDiagnostic;
    if (response.status === 401) {
      let claims: Record<string, unknown> = {};
      try { claims = JSON.parse(atob((token || "").split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); }
      catch { /* Never include a token/parser exception in evidence. */ }
      const now = Date.now() / 1000;
      const reason = payload.error?.message;
      authDiagnostic = { tokenPresent: Boolean(token),
        requestId: typeof payload.meta?.requestId === "string"
          && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(payload.meta.requestId)
          ? payload.meta.requestId : null,
        issuedAgo: typeof claims.iat === "number" ? now - claims.iat : null,
        expiresIn: typeof claims.exp === "number" ? claims.exp - now : null,
        notBeforeIn: typeof claims.nbf === "number" ? claims.nbf - now : null,
        expectedAudience: [claims.aud].flat().includes(window.__RUNTIME_CONFIG__?.keycloakClientId),
        reason: ["Missing token", "Token expired", "Invalid token"].includes(reason) ? reason : "other",
      };
    }
    return { status: response.status, body: payload, authDiagnostic };
  }, { url: `${base}${endpoint}`, method, body });
  expect(result.status, `${method} ${endpoint} ${JSON.stringify(result.authDiagnostic ?? {})}`).toBe(status);
  return result.body.data as T;
}

function graph(name: string, agent: string, nodeKey: string, autoRoutable: boolean) {
  return { name, description: "Owned routing acceptance", supervisorAgentDefinitionId: agent,
    autoRoutable, maxParallelism: 1, edges: [], nodes: [{ nodeKey, displayName: nodeKey,
      agentDefinitionId: agent, instruction: `MULTI_AGENT_NODE:${nodeKey}`, requiresConfirmation: false }] };
}

async function grant(page: Page, type: string, id: string, member: string) {
  await api(page, ADMIN_API, "/v1/admin/authorization/grants", "POST", {
    resourceType: type, resourceId: id, principalType: "user", principalId: member, permission: "read",
  }, 201);
}

test.use({ trace: "off", video: "off", screenshot: "off" });
test.skip(!ENABLED, "requires exclusive workflow-routing launcher");

test("automatic selection, malformed/inaccessible fallback and explicit picker priority", async ({ browser }) => {
  test.setTimeout(300_000);
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
    const source = await api<Workflow>(admin, ADMIN_API, `/v1/admin/workflows/${state.source_workflow_id}`);
    const automatic = await api<Workflow>(admin, ADMIN_API, `/v1/admin/workflows/${source.id}`, "PUT", {
      ...graph(`${MARKER}-source`, state.source_agent_ids.v1, "alpha", true), version: source.version,
    });
    const template = await api<Workflow>(admin, ADMIN_API, "/v1/admin/workflow-templates", "POST", {
      sourceWorkflowId: automatic.id, sourceWorkflowVersion: automatic.version, name: `${MARKER}-template`,
    }, 201);
    record("template", template.id);
    const copy = await api<{ workflow: Workflow; agentIds: string[] }>(admin, ADMIN_API,
      `/v1/admin/workflow-templates/${template.id}/instantiate`, "POST",
      { version: template.version, name: `${MARKER}-copy-a` }, 201);
    record("workflow", copy.workflow.id);
    for (const id of copy.agentIds) record("agent", id);
    expect(copy.agentIds).toHaveLength(1);
    const denied = await api<Workflow>(admin, ADMIN_API, `/v1/admin/workflows/${copy.workflow.id}`, "PUT", {
      ...graph(`${MARKER}-copy-a`, copy.agentIds[0], "beta", true), version: copy.workflow.version,
    });
    for (const [type, id] of [["workflow", automatic.id], ["agent_definition", state.source_agent_ids.v1], ["model", MODEL]]) {
      await grant(admin, type, id, state.member_id);
    }
    await grant(admin, "agent_definition", copy.agentIds[0], state.member_id);
    await member.reload();
    await member.getByRole("button", { name: "Choose model" }).click();
    await member.getByRole("option", { name: new RegExp(`^${MODEL}(?:\\s|$)`) }).click();
    const replies = member.getByTestId("assistant-content");

    async function turn(prompt: string, expected: string, workflowId?: string) {
      const count = await replies.count();
      const progressCount = await member.getByLabel("Workflow progress").count();
      const sent = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream` && r.request().method() === "POST");
      await member.getByLabel("Message").fill(prompt);
      await member.getByLabel("Send").click();
      const response = await sent;
      expect(response.status()).toBe(200);
      expect(response.request().postDataJSON().workflowId).toBe(workflowId);
      await expect(replies).toHaveCount(count + 1, { timeout: 120_000 });
      await expect(replies.last()).toHaveText(expected, { timeout: 120_000 });
      // Wait for the actual stream to end before any negative assertion.
      const stream = await response.text();
      expect(stream).not.toMatch(/^event: error\r?$/m);
      expect(stream).toMatch(/^event: done\r?$/m);
      if (expected.startsWith("MULTI_AGENT_FINAL")) {
        await expect(member.getByLabel("Workflow progress").last()).toContainText("Workflow completed");
      } else {
        expect(stream).not.toContain("workflow-run-status");
        await expect(member.getByLabel("Workflow progress")).toHaveCount(progressCount);
      }
      await expect(member.getByRole("button", { name: "Choose workflow" })).toContainText("Plain chat");
    }

    // The first turn fixes the exact title expected by the durable C5 journal.
    const created = member.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/conversations` && r.request().method() === "POST");
    const seed = turn(`${MARKER}-run-v1`, "Hello from the deterministic Kubernetes gateway.");
    const conversationResponse = await created;
    expect(conversationResponse.status()).toBe(200);
    const conversationId = (await conversationResponse.json()).data.id as string;
    record("conversation", conversationId);
    await seed;
    await turn(`ROUTING_SELECT:${automatic.id}`, "MULTI_AGENT_FINAL:alpha");
    await turn("ROUTING_MALFORMED", "ROUTING_CHAT_FALLBACK");
    await api(member, BUSINESS_API, `/v1/workflows/${copy.workflow.id}`, "GET", undefined, 404);
    await turn(`ROUTING_UNAVAILABLE:${copy.workflow.id}`, "ROUTING_CHAT_FALLBACK");
    await grant(admin, "workflow", copy.workflow.id, state.member_id);
    await api(admin, ADMIN_API, `/v1/admin/workflows/${copy.workflow.id}`, "PUT", {
      ...graph(`${MARKER}-copy-a`, copy.agentIds[0], "beta", false), version: denied.version,
    });
    // Re-open the persisted conversation through history, reloading the live
    // picker catalogue after grants. No query-cache or React state injection.
    await member.goto(`${BUSINESS}/history`);
    await member.getByRole("main").getByText(`${MARKER}-run-v1`, { exact: true }).click();
    await expect(member.getByLabel("Message")).toBeVisible();
    await member.getByRole("button", { name: "Choose workflow" }).click();
    await member.getByRole("option", { name: new RegExp(`^${MARKER}-copy-a(?:\\s|$)`) }).click();
    await turn(`ROUTING_SELECT:${automatic.id}`, "MULTI_AGENT_FINAL:beta", copy.workflow.id);
    const history = await api<{ messages: { data: { role: string; content: string }[] } }>(member, BUSINESS_API, `/v1/conversations/${conversationId}`);
    expect(history.messages.data.filter(m => m.role === "assistant").map(m => m.content)).toEqual([
      "Hello from the deterministic Kubernetes gateway.", "MULTI_AGENT_FINAL:alpha",
      "ROUTING_CHAT_FALLBACK", "ROUTING_CHAT_FALLBACK", "MULTI_AGENT_FINAL:beta",
    ]);
  } finally {
    await Promise.allSettled([adminContext.close(), memberContext.close()]);
    // Wrapper cleanup checks all owned Temporal executions before DB/runtime removal.
  }
});
