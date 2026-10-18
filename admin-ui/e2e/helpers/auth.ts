import { Page, expect } from "@playwright/test";

// Admin plane defaults to the admin user — most flows here need admin:write.
export const TEST_USER = process.env.E2E_USERNAME || "admin";
export const TEST_PASSWORD = process.env.E2E_PASSWORD || "admin";

/**
 * Drives the real Keycloak Authorization Code + PKCE redirect flow used by
 * AuthProvider (kc.login()) — no mocking, this hits the actual Keycloak
 * server started by docker-compose. Same helper as business-ui's, adapted
 * for admin-ui's landing route and default admin creds.
 */
export async function login(
  page: Page,
  username: string = TEST_USER,
  password: string = TEST_PASSWORD
): Promise<void> {
  await page.goto("/login");
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  await page.waitForURL(/\/admin\/agents$/, { timeout: 30_000 });
  // AdminNav renders regardless of the landed page's own admin:write check
  // A non-admin user still reaches /admin/agents but sees access denied
  // instead of the SkillsTable; reaching the route confirms login.
  // the app shell loaded without baking in a permission assumption.
  await expect(page.getByRole("link", { name: "Groups", exact: true })).toBeVisible();
}
