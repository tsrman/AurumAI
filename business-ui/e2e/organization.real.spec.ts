import {
  APIRequestContext,
  Browser,
  BrowserContext,
  Locator,
  Page,
  expect,
  test,
} from "@playwright/test";

const ENABLED = process.env.E2E_ORGANIZATION_UI === "true";
const BUSINESS_URL =
  process.env.E2E_ORGANIZATION_UI_BUSINESS_URL || "http://localhost:3000";
const ADMIN_URL =
  process.env.E2E_ORGANIZATION_UI_ADMIN_URL || "http://localhost:3100";
const ADMIN_API_URL =
  process.env.E2E_ORGANIZATION_UI_ADMIN_API_URL || "http://localhost:8001";
const KEYCLOAK_URL =
  process.env.E2E_ORGANIZATION_UI_KEYCLOAK_URL || "http://localhost:8080";
const MODEL = process.env.E2E_ORGANIZATION_UI_MODEL || "quota-e2e-model";
const USER_ID = process.env.E2E_ORGANIZATION_UI_USER_ID || "";
const PERIOD_FROM = process.env.E2E_ORGANIZATION_UI_PERIOD_FROM || "";
const PERIOD_TO = process.env.E2E_ORGANIZATION_UI_PERIOD_TO || "";
const GRANTS_RECONCILE_PATH = "/v1/admin/authorization/grants/reconcile";

type Grant = {
  principalType: "organization" | "group" | "user";
  principalId: string;
  permission: "read" | "write";
};

async function loginAt(
  page: Page,
  baseUrl: string,
  landing: RegExp,
  username: string,
  password: string
): Promise<void> {
  await page.goto(`${baseUrl}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  await page.waitForURL(landing, { timeout: 30_000 });
}

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
  const payload = (await response.json()) as { access_token: string };
  return payload.access_token;
}

async function listModelGrants(
  request: APIRequestContext,
  token: string
): Promise<Grant[]> {
  const response = await request.get(
    `${ADMIN_API_URL}/v1/admin/authorization/grants` +
      `?resourceType=model&resourceId=${encodeURIComponent(MODEL)}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  expect(response.status()).toBe(200);
  const payload = (await response.json()) as { data: Grant[] };
  return payload.data.map(({ principalType, principalId, permission }) => ({
    principalType,
    principalId,
    permission,
  }));
}

async function reconcileModelGrants(
  request: APIRequestContext,
  token: string,
  grants: Grant[]
): Promise<void> {
  const response = await request.put(
    `${ADMIN_API_URL}${GRANTS_RECONCILE_PATH}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      data: { resourceType: "model", resourceId: MODEL, grants },
    }
  );
  expect(response.status()).toBe(200);
}

async function closeContext(context: BrowserContext | undefined): Promise<void> {
  if (context) await context.close();
}

async function openBusinessSettings(browser: Browser): Promise<{
  context: BrowserContext;
  page: Page;
  modelSelect: Locator;
}> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await loginAt(page, BUSINESS_URL, /\/chat$/, "user", "user");
  await page.goto(`${BUSINESS_URL}/settings`);
  const modelSelect = page.getByLabel("Default model");
  await expect(modelSelect).toBeVisible();
  return { context, page, modelSelect };
}

// Playwright traces persist request headers, including Keycloak bearer
// tokens. This manual acceptance keeps only failure screenshots.
test.use({ trace: "off" });

test.describe("Organization models and analytics: full UI lifecycle", () => {
  test.skip(!ENABLED, "E2E_ORGANIZATION_UI is not enabled");

  test("default propagation, analytics, revoke and stale recovery", async ({
    browser,
    request,
  }) => {
    test.setTimeout(180_000);
    expect(USER_ID).not.toBe("");
    expect(PERIOD_FROM).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(PERIOD_TO).toMatch(/^\d{4}-\d{2}-\d{2}$/);

    let memberAdminContext: BrowserContext | undefined;
    let adminContext: BrowserContext | undefined;
    let businessContext: BrowserContext | undefined;
    let token = "";
    let originalGrants: Grant[] | undefined;
    let primaryError: unknown;

    try {
      memberAdminContext = await browser.newContext();
      const memberAdminPage = await memberAdminContext.newPage();
      await loginAt(
        memberAdminPage,
        ADMIN_URL,
        /\/admin\/users$/,
        "user",
        "user"
      );
      for (const path of ["/admin/models", "/admin/analytics"]) {
        await memberAdminPage.goto(`${ADMIN_URL}${path}`);
        await expect(
          memberAdminPage.getByText(/access denied/i)
        ).toBeVisible();
      }

      adminContext = await browser.newContext();
      businessContext = await browser.newContext();
      const adminPage = await adminContext.newPage();
      const businessPage = await businessContext.newPage();
      await loginAt(
        adminPage,
        ADMIN_URL,
        /\/admin\/users$/,
        "admin",
        "admin"
      );
      await loginAt(
        businessPage,
        BUSINESS_URL,
        /\/chat$/,
        "user",
        "user"
      );
      await expect
        .poll(() =>
          adminPage.evaluate(
            () => window.__RUNTIME_CONFIG__?.keycloakClientId ?? ""
          )
        )
        .toBe("admin-ui");
      await expect
        .poll(() =>
          businessPage.evaluate(
            () => window.__RUNTIME_CONFIG__?.keycloakClientId ?? ""
          )
        )
        .toBe("business-ui");

      await adminPage.goto(`${ADMIN_URL}/admin/models`);
      await expect(adminPage.getByText("Gateway available")).toBeVisible();
      await adminPage.getByLabel("Default model").selectOption(MODEL);
      const saved = adminPage.waitForResponse(
        (response) =>
          response.url().endsWith("/v1/admin/settings/models") &&
          response.request().method() === "PUT" &&
          response.status() === 200
      );
      await adminPage.getByRole("button", { name: "Save" }).click();
      await saved;

      await closeContext(businessContext);
      businessContext = undefined;
      let business = await openBusinessSettings(browser);
      businessContext = business.context;
      let businessModelSelect = business.modelSelect;
      await expect(
        businessModelSelect.locator('option[value=""]')
      ).toHaveText(`Organization default (${MODEL})`);
      await expect(businessModelSelect).toHaveValue("");

      await adminPage.goto(
        `${ADMIN_URL}/admin/analytics?from=${PERIOD_FROM}&to=${PERIOD_TO}`
      );
      const totals = adminPage.getByRole("region", { name: "Totals" });
      await expect(totals).toContainText(/Requests\s*3/);
      await expect(totals).toContainText(/Tokens\s*24/);
      await expect(
        adminPage.getByText("Requests remaining: 7")
      ).toBeVisible();
      await expect(
        adminPage.getByText("Tokens remaining: 76")
      ).toBeVisible();
      await expect(
        adminPage.getByText("1 request without token data")
      ).toBeVisible();
      await expect(
        adminPage.getByRole("row", { name: new RegExp(MODEL) })
      ).toBeVisible();
      await expect(
        adminPage.getByRole("row", { name: /Regular User/ })
      ).toBeVisible();

      await adminPage.getByLabel("User").selectOption(USER_ID);
      await adminPage.getByLabel("Model").fill(MODEL);
      await adminPage.getByRole("button", { name: "Apply" }).click();
      await expect(adminPage).toHaveURL(
        new RegExp(
          `userId=${encodeURIComponent(USER_ID)}.*model=${encodeURIComponent(MODEL)}`
        )
      );
      await expect(totals).toContainText(/Requests\s*3/);
      await adminPage.getByRole("button", { name: "Requests" }).click();
      await expect(
        adminPage.getByRole("img", {
          name: "Requests per day",
        })
      ).toBeVisible();
      await expect(
        adminPage.getByRole("table", {
          name: "Usage chart data",
        })
      ).toBeVisible();

      token = await adminToken(request);
      originalGrants = await listModelGrants(request, token);
      await reconcileModelGrants(request, token, []);

      await adminPage.goto(`${ADMIN_URL}/admin/models`);
      await expect(
        adminPage.getByText(
          "The saved model is no longer available to the organization."
        )
      ).toBeVisible();

      await closeContext(businessContext);
      businessContext = undefined;
      business = await openBusinessSettings(browser);
      businessContext = business.context;
      businessModelSelect = business.modelSelect;
      await expect(
        businessModelSelect.locator('option[value=""]')
      ).toHaveText(`Organization default (${MODEL})`);
      await expect(
        businessModelSelect.locator(`option[value="${MODEL}"]`)
      ).toHaveCount(0);

      await adminPage.getByLabel("Default model").selectOption("");
      const cleared = adminPage.waitForResponse(
        (response) =>
          response.url().endsWith("/v1/admin/settings/models") &&
          response.request().method() === "PUT" &&
          response.status() === 200
      );
      await adminPage.getByRole("button", { name: "Save" }).click();
      await cleared;
      await closeContext(businessContext);
      businessContext = undefined;
      business = await openBusinessSettings(browser);
      businessContext = business.context;
      businessModelSelect = business.modelSelect;
      await expect(
        businessModelSelect.locator('option[value=""]')
      ).toHaveText("Organization default");
    } catch (error) {
      primaryError = error;
      throw error;
    } finally {
      let restoreError: unknown;
      if (token && originalGrants) {
        try {
          await reconcileModelGrants(request, token, originalGrants);
        } catch (error) {
          restoreError = error;
        }
      }
      await Promise.allSettled([
        closeContext(memberAdminContext),
        closeContext(adminContext),
        closeContext(businessContext),
      ]);
      if (restoreError && !primaryError) throw restoreError;
      if (restoreError) {
        console.error(
          "organization UI E2E grant restore failed after the primary test failure"
        );
      }
    }
  });
});
