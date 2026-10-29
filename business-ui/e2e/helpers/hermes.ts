import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

/** Shared by the Hermes Agent browser suites (hermes.real*.spec.ts). */
export const BUSINESS_URL = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
export const ADMIN_UI_URL = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";
const ADMIN_API = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const KEYCLOAK_URL = process.env.E2E_KEYCLOAK_URL || "http://localhost:8080";

type Grant = { principalType: string; principalId: string; permission: string };
type HermesSettings = { egressMode: string; model: string | null };

export async function loginAt(page: Page, appUrl: string, landing: RegExp, username: string): Promise<void> {
  await page.goto(`${appUrl}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(username);
  await page.locator("#kc-login").click();
  await page.waitForURL(landing, { timeout: 30_000 });
}

export async function adminToken(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${KEYCLOAK_URL}/realms/aurum-ai/protocol/openid-connect/token`, {
    form: { grant_type: "password", client_id: "admin-ui", username: "admin", password: "admin" },
  });
  expect(response.ok()).toBeTruthy();
  return ((await response.json()) as { access_token: string }).access_token;
}

export async function adminApi<T>(
  request: APIRequestContext,
  token: string,
  method: "GET" | "PUT" | "DELETE",
  path: string,
  data?: unknown,
  status = 200
): Promise<T> {
  const response = await request.fetch(`${ADMIN_API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  expect(response.status(), `${method} ${path}`).toBe(status);
  return status === 204 ? (undefined as T) : ((await response.json()) as { data: T }).data;
}

/**
 * Lets the organization use Hermes on `model`, starting with no Hermes model
 * so an Admin UI choice is a real change; returns what puts everything back.
 */
export async function enableHermes(
  request: APIRequestContext,
  token: string,
  model: string
): Promise<() => Promise<void>> {
  const subjects = await adminApi<{ principalType: string; principalId: string }[]>(
    request, token, "GET", "/v1/admin/authorization/subjects"
  );
  const organization = subjects.find((subject) => subject.principalType === "organization");
  expect(organization).toBeDefined();
  const capabilities = (
    await adminApi<{ capabilities: string[] }>(request, token, "GET", "/v1/admin/authorization/capabilities/organization")
  ).capabilities;
  const grants = (
    await adminApi<Grant[]>(
      request, token, "GET", `/v1/admin/authorization/grants?resourceType=model&resourceId=${encodeURIComponent(model)}`
    )
  ).map(({ principalType, principalId, permission }) => ({ principalType, principalId, permission }));
  const hermes = await adminApi<HermesSettings>(request, token, "GET", "/v1/admin/settings/hermes");

  await adminApi(request, token, "PUT", "/v1/admin/authorization/capabilities/organization", {
    capabilities: [...new Set([...capabilities, "chat.send", "agents.hermes"])],
  });
  await adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
    resourceType: "model",
    resourceId: model,
    grants: [
      ...grants.filter((grant) => grant.principalType !== "organization"),
      { principalType: "organization", principalId: organization!.principalId, permission: "read" },
    ],
  });
  await adminApi(request, token, "PUT", "/v1/admin/settings/hermes", { model: null, egressMode: "none" });

  return async () => {
    // A real model's run outlasts the admin token's few minutes.
    token = await adminToken(request);
    await adminApi(request, token, "PUT", "/v1/admin/settings/hermes", {
      model: hermes.model,
      egressMode: hermes.egressMode,
    });
    await adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
      resourceType: "model",
      resourceId: model,
      grants,
    });
    await adminApi(request, token, "PUT", "/v1/admin/authorization/capabilities/organization", { capabilities });
  };
}

/** Picks the organization's Hermes model on the Admin UI's Models page. */
export async function chooseHermesModel(admin: Page, model: string): Promise<void> {
  await admin.goto(`${ADMIN_UI_URL}/admin/models`);
  const hermes = section(admin, "Hermes Agent");
  await hermes.getByLabel("Model", { exact: true }).selectOption(model);
  await hermes.getByLabel("Internet access").selectOption("none");
  const saved = admin.waitForResponse(
    (response) =>
      response.url().endsWith("/v1/admin/settings/hermes") &&
      response.request().method() === "PUT" &&
      response.status() === 200
  );
  await hermes.getByRole("button", { name: "Save Hermes Agent settings" }).click();
  await saved;
}

export async function chooseHermes(member: Page): Promise<void> {
  await member.getByRole("button", { name: "Choose workflow" }).click();
  await member.getByRole("option", { name: /^Hermes Agent/ }).click();
}

/** Sends a message and waits for the answer that follows it. */
export async function ask(page: Page, text: string, expected: string | RegExp, timeout: number): Promise<void> {
  const replies = page.getByTestId("assistant-content");
  const before = await replies.count();
  await expect(page.getByLabel("Message")).toBeEnabled({ timeout });
  await page.getByLabel("Message").fill(text);
  await page.getByLabel("Send").click();
  await expect(replies).toHaveCount(before + 1, { timeout });
  await expect(replies.last()).toContainText(expected, { timeout });
}

export function section(page: Page, title: string): Locator {
  return page.getByRole("heading", { name: title, exact: true }).locator("xpath=ancestor::section[1]");
}
