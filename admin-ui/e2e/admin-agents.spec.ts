import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

// Must be a model the stack's gateway publishes and the organization may use.
const MODEL = process.env.E2E_AGENT_MODEL || process.env.E2E_WORKFLOW_CANVAS_MODEL || "gpt-4o-mini";

/**
 * /admin/agents end to end against the real agent-definitions router on the
 * admin backend: create through the editor, edit (PATCH), then delete.
 */
test.describe("Admin: agents", () => {
  test("a user without the administrator role sees access denied", async ({ page }) => {
    await login(page, "user", "user");
    await page.goto("/admin/agents");
    await expect(page.getByText(/access denied/i)).toBeVisible();
    await expect(page.getByRole("link", { name: "New agent" })).toHaveCount(0);
  });

  test("admin creates, edits and deletes an agent", async ({ page }) => {
    await login(page, "admin", "admin");
    await page.goto("/admin/agents");
    await page.getByRole("link", { name: "New agent" }).first().click();
    await expect(page).toHaveURL(/\/admin\/agents\/new$/);

    const name = `e2e-agent-${Date.now()}`;
    await page.getByLabel("Agent name").fill(name);
    await page.getByLabel("Short description").fill("Created by e2e");
    // The picker trigger is named after the current model.
    await page.locator('button[aria-haspopup="listbox"]').click();
    await page.getByRole("listbox", { name: "Choose model" }).getByRole("option", { name: MODEL, exact: true }).click();
    await page.getByRole("button", { name: "Create agent" }).click();

    await expect(page).toHaveURL(/\/admin\/agents$/);
    const row = page.locator("tr", { hasText: name });
    await expect(row).toBeVisible();
    await expect(row.getByText("Created by e2e")).toBeVisible();

    await row.getByRole("button", { name: `Edit ${name}`, exact: true }).click();
    await expect(page.getByRole("heading", { name: `Agent “${name}”` })).toBeVisible();
    await expect(page.getByLabel("Short description")).toHaveValue("Created by e2e");
    await page.getByLabel("Short description").fill("Edited by e2e");
    await page.getByRole("button", { name: "Save changes" }).click();

    await expect(page).toHaveURL(/\/admin\/agents$/);
    const edited = page.locator("tr", { hasText: name });
    await expect(edited.getByText("Edited by e2e")).toBeVisible();

    await edited.getByRole("button", { name: `Delete ${name}`, exact: true }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Delete", exact: true }).click();
    await expect(page.locator("tr", { hasText: name })).toHaveCount(0);
  });
});
