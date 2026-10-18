import { Page, expect } from "@playwright/test";

export const TEST_USER = process.env.E2E_USERNAME || "user";
export const TEST_PASSWORD = process.env.E2E_PASSWORD || "user";

/**
 * Drives the real Keycloak Authorization Code + PKCE redirect flow used by
 * AuthProvider (kc.login()) — no mocking, this hits the actual Keycloak
 * server started by docker-compose.
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
  await page.waitForURL(/\/chat$/, { timeout: 30_000 });
  await expect(page.getByLabel("Message")).toBeVisible();
}
