import { expect, test as base, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Dedicated disposable project only. Never accepts production URL/container overrides.
const enabled = process.env.E2E_DATA_LIFECYCLE === "true";
const project = "phaseb-lifecycle-test";
let phase = "setup";
const test = base.extend<{ privateErrors: void }>({
  privateErrors: [async ({}, use, info) => {
    await use();
    for (const error of info.errors) {
      for (const key of Object.keys(error)) Reflect.deleteProperty(error, key);
      error.message = `[lifecycle-phase:${phase}] Private acceptance assertion failed; content diagnostics suppressed.`;
      error.stack = error.message;
    }
  }, { auto: true }],
});
test.skip(!enabled, "Requires the owned data-lifecycle acceptance project");
test.describe.configure({ mode: "serial", timeout: 120_000 });

function ownedContainer(service: string): string {
  const name = `${project}-${service}-1`;
  const [container] = JSON.parse(execFileSync("docker", ["inspect", name], { encoding: "utf8" }));
  if (container.Config.Labels["com.docker.compose.project"] !== project ||
      container.Config.Labels["com.docker.compose.service"] !== service) {
    throw new Error("Acceptance container ownership mismatch");
  }
  if (container.State?.Running !== true) throw new Error("Owned service is stopped");
  const ports: Record<string, [string, string]> = {
    postgres: ["5432/tcp", "55434"], keycloak: ["8080/tcp", "18080"],
    frontend: ["3000/tcp", "13000"], "admin-ui": ["3000/tcp", "13100"],
    backend: ["8000/tcp", "18000"], "backend-admin": ["8001/tcp", "18001"],
  };
  if (ports[service]) {
    const [port, hostPort] = ports[service];
    const bindings = container.NetworkSettings.Ports[port];
    if (bindings?.length !== 1 || bindings[0].HostIp !== "127.0.0.1" || bindings[0].HostPort !== hostPort) {
      throw new Error("Owned service binding mismatch");
    }
  }
  return container.Id as string;
}

test.beforeEach(() => {
  for (const service of ["postgres", "keycloak", "frontend", "admin-ui", "backend", "backend-admin"]) {
    ownedContainer(service);
  }
});

function membershipCommand(command: "prepare" | "revoke" | "recover"): string {
  return execFileSync("python3", [
    resolve(__dirname, "../../scripts/data-lifecycle-membership.py"), command,
  ], { encoding: "utf8", stdio: "pipe" });
}

async function login(page: Page, admin = false, username?: string) {
  const port = admin ? 13100 : 13000;
  const account = username || (admin ? "admin" : "user");
  await page.goto(`http://127.0.0.1:${port}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.locator("#username").fill(account);
  await page.locator("#password").fill(account);
  await page.locator("#kc-login").click();
  await expect.poll(() => new URL(page.url()).pathname).toBe(admin ? "/admin/agents" : "/chat");
  await expect.poll(() => page.evaluate(() => Boolean(window.keycloak?.token))).toBe(true);
}

async function apiStatus(page: Page, admin = false) {
  return page.evaluate(async (admin) => {
    const token = window.keycloak?.token;
    if (!token) return 0;
    const url = admin ? "http://127.0.0.1:18001/v1/admin/groups" : "http://127.0.0.1:18000/v1/conversations";
    const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    return response.status;
  }, admin);
}

function pendingIdentities(): number {
  const sql = "SELECT count(*) FROM identity_sync_state WHERE applied_generation < security_generation";
  return Number(execFileSync("docker", ["exec", ownedContainer("postgres"), "psql", "-U", "postgres", "-d", "aurum_ai", "-Atc", sql], { encoding: "utf8" }).trim());
}

test("both Keycloak audiences complete background admission", async ({ browser }) => {
  for (const admin of [false, true]) {
    const context = await browser.newContext();
    try {
      const page = await context.newPage();
      await login(page, admin);
      await expect.poll(() => apiStatus(page, admin), { timeout: 30_000 }).toBe(200);
      for (let i = 0; i < 3; i++) expect(await apiStatus(page, admin)).toBe(200);
      expect(pendingIdentities()).toBe(0);
    } finally {
      await context.close();
    }
  }
});

test("worker outage withholds a new identity until durable inbox recovery", async ({ browser }) => {
  const worker = ownedContainer("identity-worker");
  const context = await browser.newContext();
  execFileSync("docker", ["stop", worker], { stdio: "pipe" });
  try {
    const page = await context.newPage();
    await login(page, false, "user2");
    expect(await apiStatus(page)).toBe(503);
    expect(pendingIdentities()).toBeGreaterThan(0);
    execFileSync("docker", ["start", worker], { stdio: "pipe" });
    await expect.poll(() => apiStatus(page), { timeout: 40_000 }).toBe(200);
    expect(pendingIdentities()).toBe(0);
  } finally {
    execFileSync("docker", ["start", worker], { stdio: "pipe" });
    await context.close();
  }
});

test("changed Keycloak groups wait for application and revoke old membership", async ({ browser }) => {
  phase = "group-admin-login";
  const context = await browser.newContext();
  const worker = ownedContainer("identity-worker");
  let journalled = false;
  try {
    phase = "group-inventory";
    const membership = JSON.parse(membershipCommand("prepare"));
    journalled = true;
    expect(membership.was_member).toBe(true);
    if (!/^[0-9a-f-]{36}$/.test(membership.user_id)) throw new Error("Invalid owned identity");
    phase = "group-user-login";
    const page = await context.newPage();
    await login(page);
    await expect.poll(() => apiStatus(page)).toBe(200);
    execFileSync("docker", ["stop", worker], { stdio: "pipe" });
    phase = "group-revoke";
    membershipCommand("revoke");
    // Real second boundary: different security snapshot must have a newer iat.
    await new Promise((resolve) => setTimeout(resolve, 1100));
    phase = "group-token-refresh";
    await page.evaluate(async () => { await window.keycloak?.updateToken(-1); });
    phase = "group-marker-contract";
    const claimShape = await page.evaluate(() => {
      const token = window.keycloak?.token;
      if (!token) return { complete: false, groupsPresent: false };
      const claims = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      return { complete: claims.platform_groups_complete === true, groupsPresent: Object.hasOwn(claims, "groups") };
    });
    expect(claimShape).toEqual({ complete: true, groupsPresent: false });
    phase = "group-pending-admission";
    expect(await apiStatus(page)).toBe(503);
    execFileSync("docker", ["start", worker], { stdio: "pipe" });
    phase = "group-recovered-admission";
    await expect.poll(() => apiStatus(page), { timeout: 40_000 }).toBe(200);
    const sql = "SELECT count(*) FROM user_groups ug JOIN users u ON u.id=ug.user_id JOIN groups g ON g.id=ug.group_id WHERE u.keycloak_id='" + membership.user_id + "' AND g.name='analysts'";
    phase = "group-database-revoke";
    expect(Number(execFileSync("docker", ["exec", ownedContainer("postgres"), "psql", "-U", "postgres", "-d", "aurum_ai", "-Atc", sql], { encoding: "utf8" }).trim())).toBe(0);
  } finally {
    try {
      execFileSync("docker", ["start", worker], { stdio: "pipe" });
      if (journalled) membershipCommand("recover");
    } finally {
      await context.close();
    }
  }
});

test("archive history visibility follows the operator lifecycle", async ({ page }) => {
  const stage = process.env.E2E_DATA_LIFECYCLE_ARCHIVE_STAGE;
  test.skip(!stage, "Run explicitly before archive, after purge, and after operator restore");
  if (!["before", "archived", "restored"].includes(stage!)) throw new Error("Invalid archive stage");
  phase = `archive-${stage}-ownership`;
  const state = JSON.parse(readFileSync("/tmp/phaseb-lifecycle-acceptance/archive-fixture.json", "utf8"));
  expect(state.postgres_container_id).toBe(ownedContainer("postgres"));
  if (!/^[0-9a-f-]{36}$/.test(state.conversation_id)) throw new Error("Invalid fixture identity");
  phase = `archive-${stage}-login`;
  await login(page);
  await expect.poll(() => apiStatus(page)).toBe(200);
  if (state.attachment) {
    phase = `archive-${stage}-attachment`;
    for (const id of [state.attachment.document_id, state.attachment.knowledge_base_id]) {
      if (!/^[0-9a-f-]{36}$/.test(id)) throw new Error("Invalid attachment fixture identity");
    }
    // The existing chat only shows upload chips during composition. Check the
    // persisted attachment through the same authenticated business API instead.
    const attachment = await page.evaluate(async ({ documentId, knowledgeBaseId }) => {
      const response = await fetch(
        `http://127.0.0.1:18000/v1/documents/${documentId}?knowledge_base_id=${knowledgeBaseId}`,
        { headers: { Authorization: `Bearer ${window.keycloak?.token}` } },
      );
      if (response.status !== 200) return { status: response.status };
      const { data } = await response.json();
      return {
        status: response.status, id: data.id, knowledgeBaseId: data.knowledge_base_id,
        filename: data.source_filename, documentStatus: data.status, chunkCount: data.chunk_count,
      };
    }, { documentId: state.attachment.document_id, knowledgeBaseId: state.attachment.knowledge_base_id });
    if (stage === "archived") {
      expect(attachment.status).toBe(404);
    } else {
      expect(attachment).toMatchObject({
        status: 200, id: state.attachment.document_id,
        knowledgeBaseId: state.attachment.knowledge_base_id,
        filename: state.attachment.filename, documentStatus: "indexed",
      });
      expect(attachment.chunkCount).toBeGreaterThan(0);
    }
  }
  phase = `archive-${stage}-history`;
  const loaded = page.waitForResponse((response) =>
    response.url().startsWith("http://127.0.0.1:18000/v1/conversations") &&
    response.request().method() === "GET" && response.status() === 200);
  await page.goto("http://127.0.0.1:13000/history");
  phase = `archive-${stage}-history-response`;
  await loaded;
  const dialogButton = { name: "Open conversation Lifecycle archive acceptance — 26 historical turns", exact: true };
  const item = page.getByRole("main").getByRole("button", dialogButton);
  if (stage === "archived") {
    await expect(page.getByRole("button", dialogButton)).toHaveCount(0);
    const status = await page.evaluate(async (id: string) => {
      const response = await fetch(`http://127.0.0.1:18000/v1/conversations/${id}`, {
        headers: { Authorization: `Bearer ${window.keycloak?.token}` },
      });
      return response.status;
    }, state.conversation_id);
    expect(status).toBe(404);
    return;
  }
  phase = `archive-${stage}-history-item`;
  await expect(item).toBeVisible();
  phase = `archive-${stage}-history-open`;
  await item.click();
  phase = `archive-${stage}-history-count`;
  await expect(page.getByTestId("assistant-content")).toHaveCount(26);
  for (let index = 0; index < 26; index++) {
    phase = `archive-${stage}-history-content-${index + 1}`;
    await expect(page.getByTestId("assistant-content").nth(index)).toHaveText(
      `Archive fixture answer ${String(index + 1).padStart(2, "0")}`,
    );
  }
  phase = `archive-${stage}-reload`;
  await page.reload();
  await expect(page.getByTestId("assistant-content")).toHaveCount(26);
  await expect(page.getByTestId("assistant-content").last()).toHaveText("Archive fixture answer 26");
});
