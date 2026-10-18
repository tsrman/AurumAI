import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

test.describe("Authentication through Keycloak", () => {
  test("an unauthenticated user on / lands on /login", async ({ page }) => {
    await page.goto("/");
    await page.waitForURL(/\/login$/);
    await expect(
      page.getByRole("button", { name: /sign in with keycloak/i })
    ).toBeVisible();
  });

  test("Keycloak login leads to /chat", async ({ page }) => {
    await login(page);
    await expect(page).toHaveURL(/\/chat$/);
  });

  test("after login a page reload keeps the session (SSO)", async ({ page }) => {
    await login(page);
    await page.reload();
    await expect(page.getByLabel("Message")).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/\/chat$/);
  });
});
