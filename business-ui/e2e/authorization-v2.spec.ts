import { APIRequestContext, Browser, expect, test } from "@playwright/test";
import { login } from "./helpers/auth";

const KEYCLOAK_URL = process.env.KEYCLOAK_URL || "http://localhost:8080";
const ADMIN_URL = process.env.ADMIN_BASE_URL || "http://localhost:8001";
const MODEL = process.env.GROUPS_SMOKE_MODEL || "gpt-4o-mini";

type Grant = {
  principalType: "organization" | "group" | "user";
  principalId: string;
  permission: "read" | "write";
};

async function passwordToken(
  request: APIRequestContext,
  username: string,
  clientId: "admin-ui" | "business-ui"
): Promise<string> {
  const response = await request.post(
    `${KEYCLOAK_URL}/realms/aurum-ai/protocol/openid-connect/token`,
    {
      form: {
        grant_type: "password",
        client_id: clientId,
        username,
        password: username === "user2" ? "user2" : username,
      },
    }
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()).access_token as string;
}

async function adminJson(
  request: APIRequestContext,
  token: string,
  method: "GET" | "PUT",
  path: string,
  data?: unknown
): Promise<unknown> {
  const response = await request.fetch(`${ADMIN_URL}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  expect(response.ok(), `${method} ${path}: ${await response.text()}`).toBeTruthy();
  return response.json();
}

async function modelOptions(browser: Browser, username: "user" | "user2") {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await login(page, username, username);
    await page.goto("/settings");
    const select = page.getByLabel("Default model");
    await expect(select).toBeVisible();
    return await select.locator("option").allTextContents();
  } finally {
    await context.close();
  }
}

test.describe("Authorization v2: business UI", () => {
  test("a grant and its revocation change visible models without anonymous access", async ({
    browser,
    page,
    request,
  }) => {
    await page.goto("/settings");
    await expect(page).toHaveURL(/\/login$/);

    const adminToken = await passwordToken(request, "admin", "admin-ui");
    const subjectsBody = (await adminJson(
      request,
      adminToken,
      "GET",
      "/v1/admin/authorization/subjects"
    )) as { data: Array<{ principalType: string; principalId: string; label: string }> };
    const user = subjectsBody.data.find(
      (subject) => subject.principalType === "user" && subject.label === "Regular User"
    );
    expect(user).toBeDefined();

    const query = `?resourceType=model&resourceId=${encodeURIComponent(MODEL)}`;
    const originalBody = (await adminJson(
      request,
      adminToken,
      "GET",
      `/v1/admin/authorization/grants${query}`
    )) as { data: Grant[] };
    const original = originalBody.data.map(({ principalType, principalId, permission }) => ({
      principalType,
      principalId,
      permission,
    }));

    const reconcile = async (grants: Grant[]) => {
      await adminJson(
        request,
        adminToken,
        "PUT",
        "/v1/admin/authorization/grants/reconcile",
        { resourceType: "model", resourceId: MODEL, grants }
      );
    };

    try {
      await reconcile([
        {
          principalType: "user",
          principalId: user?.principalId || "",
          permission: "read",
        },
      ]);

      expect(await modelOptions(browser, "user")).toContain(MODEL);
      expect(await modelOptions(browser, "user2")).not.toContain(MODEL);

      await reconcile([]);
      expect(await modelOptions(browser, "user")).not.toContain(MODEL);
    } finally {
      await reconcile(original);
    }
  });
});
