import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

const ENABLED = process.env.E2E_WORKFLOW_TEMPLATES_UI === "true";
const STATE = process.env.E2E_WORKFLOW_TEMPLATES_UI_STATE || "";
const MARKER = process.env.E2E_WORKFLOW_TEMPLATES_UI_MARKER || "";
const ADMIN = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";
const BUSINESS = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const API = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const MODEL = `${MARKER}-model`;
const ROOT = path.resolve(__dirname, "../..");
const TEMPLATES = "/v1/admin/workflow-templates";

type Fixture = {
  marker: string;
  member_id: string;
  source_workflow_id: string;
  source_agent_ids: { v1: string; v2: string };
};
type Workflow = {
  id: string;
  version: number;
  autoRoutable: boolean;
  sourceTemplateId: string;
  sourceTemplateVersion: number;
  supervisorAgentDefinitionId: string;
  nodes: { nodeKey: string; agentDefinitionId: string; instruction: string; requiresConfirmation: boolean }[];
  edges: { sourceNodeKey: string; targetNodeKey: string; condition: string }[];
};
type Copy = { workflow: Workflow; agentIds: string[] };

function record(kind: "template" | "workflow" | "agent" | "conversation", id: string) {
  execFileSync("python3", [path.join(ROOT, "scripts/workflow_templates_ui_e2e.py"),
    "record", "--state", STATE, "--marker", MARKER, "--kind", kind, "--id", id],
  { cwd: ROOT, env: process.env, stdio: "inherit" });
}

async function login(page: Page, admin: boolean) {
  const url = admin ? ADMIN : BUSINESS;
  await page.goto(`${url}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(admin
    ? process.env.E2E_ADMIN_USERNAME || "admin" : process.env.E2E_MEMBER_USERNAME || "user");
  await page.locator("#password").fill(admin
    ? process.env.E2E_ADMIN_PASSWORD || "admin" : process.env.E2E_MEMBER_PASSWORD || "user");
  await page.locator("#kc-login").click();
  await page.waitForURL(`${url}${admin ? "/admin/users" : "/chat"}`, { timeout: 30_000 });
}

// Tokens stay inside the authenticated browser; no headers, JWTs or traces are persisted.
// Only read assertions and the explicitly expected rejected archive-copy POST use this helper.
async function api(page: Page, endpoint: string, rejectedCopy?: { version: number; name: string }) {
  return page.evaluate(async ({ url, rejectedCopy }) => {
    const keycloak = (window as Window & { keycloak?: { token?: string } }).keycloak;
    const response = await fetch(url, {
      method: rejectedCopy ? "POST" : "GET",
      headers: { Authorization: `Bearer ${keycloak?.token}`, "Content-Type": "application/json" },
      ...(rejectedCopy ? { body: JSON.stringify(rejectedCopy) } : {}),
    });
    return { status: response.status, body: await response.json() };
  }, { url: `${API}${endpoint}`, rejectedCopy });
}

async function readData<T>(page: Page, endpoint: string): Promise<T> {
  const result = await api(page, endpoint);
  expect(result.status).toBe(200);
  return result.body.data as T;
}

function mutation(page: Page, endpoint: string, method = "POST") {
  return page.waitForResponse(response => response.url() === `${API}${endpoint}`
    && response.request().method() === method);
}

const RESOURCE_PAGES: Record<string, string> = {
  model: "/admin/models",
  workflow: "/admin/workflows",
  agent_definition: "/admin/agents",
};

// Fail within seconds, naming the page state, instead of idling until the
// five-minute test timeout with no usable artifact.
async function openResourceList(page: Page, type: string) {
  await page.goto(`${ADMIN}${RESOURCE_PAGES[type]}`);
  const table = page.getByRole("table").first();
  try {
    await expect(table).toBeVisible({ timeout: 45_000 });
  } catch {
    const state = await page.getByText("Checking access...").isVisible()
      ? "auth initialization still pending"
      : await page.getByText(/Access denied/).isVisible()
        ? "admin role not present in session"
        : "resource list not rendered";
    throw new Error(`Admin resource list unavailable: ${state} (${page.url()})`);
  }
}

async function resourceLabel(page: Page, type: string, id: string): Promise<string> {
  if (type === "workflow") return (await readData<{ name: string }>(page, `/v1/admin/workflows/${id}`)).name;
  if (type === "agent_definition") return (await readData<{ name: string }>(page, `/v1/agent-definitions/${id}`)).name;
  return id;
}

async function grant(page: Page, type: string, id: string, memberId: string) {
  await openResourceList(page, type);
  const label = await resourceLabel(page, type, id);
  if (type === "workflow") {
    await page.locator("tr", { hasText: label }).getByRole("button", { name: "Access", exact: true }).click();
  } else {
    await page.getByRole("button", { name: `Access to ${label}`, exact: true }).click();
  }
  const editor = page.getByRole("dialog", { name: `Access to “${label}”` });
  await expect(editor.getByText("Loading grants...")).toHaveCount(0);
  await expect(editor.getByRole("button", { name: "Save grants" })).toBeEnabled();
  await editor.getByLabel("Recipient of the new grant").selectOption(`user:${memberId}`);
  await editor.getByLabel("Access level of the new grant").selectOption("read");
  await editor.getByRole("button", { name: "Add grant" }).click();
  const saved = mutation(page, "/v1/admin/authorization/grants/reconcile", "PUT");
  await editor.getByRole("button", { name: "Save grants" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Apply" }).click();
  expect((await saved).status()).toBe(200);
  await expect(page.getByRole("alertdialog")).not.toBeVisible();
}

async function noGrants(page: Page, copy: Copy) {
  for (const [type, id] of [["workflow", copy.workflow.id],
    ...copy.agentIds.map(id => ["agent_definition", id])]) {
    expect(await readData(page,
      `/v1/admin/authorization/grants?resourceType=${type}&resourceId=${id}`)).toEqual([]);
  }
}

async function copyTemplate(page: Page, templateId: string, suffix: "a" | "b"): Promise<Copy> {
  await page.getByRole("tab", { name: "Saved templates", exact: true }).click();
  await page.getByLabel("Workflow template", { exact: true }).selectOption(templateId);
  await page.getByLabel("Workflow copy name").fill(`${MARKER}-copy-${suffix}`);
  const created = mutation(page, `${TEMPLATES}/${templateId}/instantiate`);
  await page.getByRole("button", { name: "Create workflow from template" }).click();
  const response = await created;
  expect(response.status()).toBe(201);
  const copy = (await response.json()).data as Copy;
  record("workflow", copy.workflow.id);
  for (const id of copy.agentIds) record("agent", id);
  await expect(page.getByRole("heading", { name: "Edit workflow", exact: true })).toBeVisible();
  return copy;
}

async function assertCopy(page: Page, copy: Copy, templateId: string, version: 1 | 2) {
  expect(copy.agentIds).toHaveLength(1); // supervisor + two nodes deduplicate one source agent
  expect(copy.workflow).toMatchObject({ version: 1, autoRoutable: false,
    sourceTemplateId: templateId, sourceTemplateVersion: version,
    supervisorAgentDefinitionId: copy.agentIds[0] });
  expect(copy.workflow.nodes.map(node => node.agentDefinitionId)).toEqual([copy.agentIds[0], copy.agentIds[0]]);
  expect(copy.workflow.nodes).toEqual(expect.arrayContaining([
    expect.objectContaining({ nodeKey: "alpha", requiresConfirmation: true,
      instruction: `MULTI_AGENT_NODE:alpha${version === 2 ? " configuration-v2" : ""}` }),
    expect.objectContaining({ nodeKey: "beta", requiresConfirmation: false,
      instruction: `MULTI_AGENT_NODE:beta${version === 2 ? " configuration-v2" : ""}` }),
  ]));
  expect(copy.workflow.edges).toEqual([expect.objectContaining({
    sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" })]);
  const agent = await readData(page, `/v1/agent-definitions/${copy.agentIds[0]}`);
  expect(agent).toMatchObject({ modelId: MODEL, skillIds: [], knowledgeBaseIds: [],
    systemPrompt: `Deterministic template source configuration v${version}.`,
    temperature: version === 1 ? 0.1 : 0.7 });
}

async function runCopy(page: Page, suffix: "v1" | "v2") {
  await page.goto(`${BUSINESS}/chat`);
  await expect(page.getByLabel("Message")).toBeVisible();
  await page.getByLabel("Sidebar").getByRole("button", { name: "New chat" }).click();
  for (const [picker, name] of [["Choose model", MODEL], ["Choose workflow", `${MARKER}-copy-a`]]) {
    await page.getByRole("button", { name: picker }).click();
    await page.getByRole("option", { name: new RegExp(`^${name}(?:\\s|$)`) }).click();
  }
  const created = page.waitForResponse(response => /\/v1\/conversations$/.test(response.url())
    && response.request().method() === "POST");
  await page.getByLabel("Message").fill(`${MARKER}-run-${suffix}`);
  await page.getByLabel("Send").click();
  const conversation = await created;
  expect(conversation.status()).toBe(200);
  record("conversation", (await conversation.json()).data.id);
  const progress = page.getByLabel("Workflow progress").last();
  await expect(progress.getByRole("button", { name: "alpha: approve", exact: true })).toBeVisible({ timeout: 90_000 });
  await progress.getByRole("button", { name: "alpha: approve", exact: true }).click();
  await expect(progress).toContainText("Workflow completed", { timeout: 120_000 });
  await expect(page.getByTestId("assistant-content").last()).toContainText("MULTI_AGENT_FINAL:alpha,beta");
}

test.use({ trace: "off" });
test.describe("Workflow templates: independent copies through real UI", () => {
  test.skip(!ENABLED, "requires exclusive workflow-templates live wrapper");
  test("capture, no inherited grants, HITL, replace, archive and run existing copy", async ({ browser }) => {
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
      await admin.goto(`${ADMIN}/admin/workflows`);
      const sourceRow = admin.locator("tr", { hasText: `${MARKER}-source` });
      await sourceRow.getByRole("button", { name: "Save as template" }).click();
      const panel = admin.getByRole("heading", { name: "Workflow templates" }).locator("../..");
      await panel.getByLabel("Template name").fill(`${MARKER}-template`);
      const captured = mutation(admin, TEMPLATES);
      await panel.getByRole("button", { name: "Save as template", exact: true }).click();
      const capturedResponse = await captured;
      expect(capturedResponse.status()).toBe(201);
      const template = (await capturedResponse.json()).data as { id: string; version: number };
      record("template", template.id);
      expect(template.version).toBe(1);
      const copyA = await copyTemplate(admin, template.id, "a");
      expect(copyA.agentIds[0]).not.toBe(state.source_agent_ids.v1);
      await assertCopy(admin, copyA, template.id, 1);
      await noGrants(admin, copyA);
      // The composer, including the workflow picker, stays disabled until the
      // member can use a model; grant it first so only the workflow grant decides.
      await grant(admin, "model", MODEL, state.member_id);
      await member.reload();
      await member.getByRole("button", { name: "Choose workflow" }).click();
      await expect(member.getByRole("option", { name: `${MARKER}-copy-a` })).toHaveCount(0);
      await member.keyboard.press("Escape");
      await grant(admin, "workflow", copyA.workflow.id, state.member_id);
      await grant(admin, "agent_definition", copyA.agentIds[0], state.member_id);
      await runCopy(member, "v1");

      await admin.goto(`${ADMIN}/admin/workflows`);
      await sourceRow.getByRole("button", { name: "Edit", exact: true }).click();
      // Wait for the selected saved definition before changing the editor.
      await expect(admin.getByRole("heading", { name: "Edit workflow", exact: true })).toBeVisible();
      await expect(admin.getByLabel("Workflow name", { exact: true })).toHaveValue(`${MARKER}-source`);
      await admin.getByRole("button", { name: "2. Agents", exact: true }).click();
      await expect(admin.getByLabel("Supervisor", { exact: true })).toHaveValue(state.source_agent_ids.v1);
      await admin.getByLabel("Supervisor", { exact: true }).selectOption(state.source_agent_ids.v2);
      // Only the selected node's fields appear in the canvas inspector — click
      // each node's card before touching its fields, unlike the old inline
      // fieldset-per-node list where every node's fields were always visible.
      await admin.getByRole("button", { name: "3. Steps", exact: true }).click();
      const confirmationChecked = [true, false];
      for (const [position, [index, key]] of ([[1, "alpha"], [2, "beta"]] as const).entries()) {
        await admin.getByLabel(`Node ${key}`, { exact: true }).click();
        await admin.getByLabel(`Node ${index} agent`, { exact: true }).selectOption(state.source_agent_ids.v2);
        await admin.getByLabel(`Node ${index} instruction`, { exact: true }).fill(`MULTI_AGENT_NODE:${key} configuration-v2`);
        if (confirmationChecked[position]) {
          await expect(admin.getByLabel(`Requires confirmation — node ${index}`)).toBeChecked();
        } else {
          await expect(admin.getByLabel(`Requires confirmation — node ${index}`)).not.toBeChecked();
        }
      }
      // Presence of the edge's condition control is itself the assertion that
      // the alpha -> beta dependency loaded from the existing definition — the
      // canvas has no separate "included or not" checkbox the way the old
      // dependency matrix did.
      await expect(admin.getByRole("combobox", { name: "Link condition alpha → beta", exact: true })).toHaveValue("on_success");
      await admin.getByRole("button", { name: "4. Check", exact: true }).click();
      await admin.getByRole("button", { name: "Validate", exact: true }).click();
      const updated = mutation(admin, `/v1/admin/workflows/${state.source_workflow_id}`, "PUT");
      await admin.getByRole("button", { name: "Save", exact: true }).click();
      const sourceResponse = await updated;
      expect(sourceResponse.status()).toBe(200);
      const updatedSource = (await sourceResponse.json()).data as Workflow;
      expect(updatedSource.supervisorAgentDefinitionId).toBe(state.source_agent_ids.v2);
      expect(updatedSource.nodes.map(node => node.agentDefinitionId)).toEqual([
        state.source_agent_ids.v2, state.source_agent_ids.v2,
      ]);
      await admin.getByRole("button", { name: "Back to workflows", exact: true }).click();
      await sourceRow.getByRole("button", { name: "Save as template" }).click();
      await admin.getByLabel("Workflow template", { exact: true }).selectOption(template.id);
      const replaced = mutation(admin, `${TEMPLATES}/${template.id}`, "PUT");
      await admin.getByRole("button", { name: "Update from workflow" }).click();
      const replacedResponse = await replaced;
      expect(replacedResponse.status()).toBe(200);
      expect((await replacedResponse.json()).data.version).toBe(2);
      copyA.workflow = await readData<Workflow>(admin, `/v1/admin/workflows/${copyA.workflow.id}`);
      await assertCopy(admin, copyA, template.id, 1);
      const copyB = await copyTemplate(admin, template.id, "b");
      expect(copyB.workflow.id).not.toBe(copyA.workflow.id);
      expect([...Object.values(state.source_agent_ids), ...copyA.agentIds]).not.toContain(copyB.agentIds[0]);
      await assertCopy(admin, copyB, template.id, 2);
      await noGrants(admin, copyB);
      await admin.getByRole("tab", { name: "Saved templates", exact: true }).click();
      await admin.getByLabel("Workflow template", { exact: true }).selectOption(template.id);
      const archived = mutation(admin, `${TEMPLATES}/${template.id}/archive`);
      await admin.getByRole("button", { name: "Archive template" }).click();
      const archivedResponse = await archived;
      expect(archivedResponse.status()).toBe(200);
      const archivedTemplate = (await archivedResponse.json()).data as { version: number; status: string };
      expect(archivedTemplate.status).toBe("archived");
      await admin.getByLabel("Show archived").check();
      await admin.getByLabel("Workflow template", { exact: true }).selectOption(template.id);
      await expect(admin.getByLabel("Workflow copy name")).toBeDisabled();
      await expect(admin.getByRole("button", { name: "Create workflow from template" })).toBeDisabled();
      const rejected = await api(admin, `${TEMPLATES}/${template.id}/instantiate`, {
        version: archivedTemplate.version, name: `${MARKER}-copy-b`,
      });
      expect(rejected.status).toBe(409);
      expect(rejected.body.error.code).toBe("template_archived");
      await runCopy(member, "v2");
    } finally {
      await Promise.allSettled([adminContext.close(), memberContext.close()]);
      // The wrapper owns cleanup and its exit status, including failures before browser startup.
    }
  });
});
