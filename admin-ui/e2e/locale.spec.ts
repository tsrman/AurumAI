import { expect, test } from "@playwright/test";
import ru from "../messages/ru.json";

// The rest of the suite pins en-US; this spec covers language negotiation.
test.describe("interface language", () => {
  test.use({ locale: "ru-RU" });

  test("a Russian browser gets the Russian interface", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator("html")).toHaveAttribute("lang", "ru");
    await expect(page.getByRole("button", { name: ru.login.signIn })).toBeVisible();
  });

  test("the language cookie overrides the browser language", async ({ page, context, baseURL }) => {
    await context.addCookies([{ name: "NEXT_LOCALE", value: "en", url: baseURL ?? "http://localhost" }]);
    await page.goto("/login");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page.getByRole("button", { name: "Sign in with Keycloak" })).toBeVisible();
  });
});
