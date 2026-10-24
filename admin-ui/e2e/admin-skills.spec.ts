import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

/**
 * /admin/skills is real end-to-end: useAdminAccess() is a thin wrapper over
 * the authenticated organization role, and the
 * SkillFormDialog/SkillsTable hit the real /v1/skills + /v1/skills/{id}
 * (read) and /v1/skills (write, skills_admin.py) routers on the admin
 * backend (:8001). No stubbing.
 */
test.describe("Admin: skills", () => {
  test("a user without admin:write sees access denied", async ({ page }) => {
    await login(page, "user", "user");
    await page.goto("/admin/skills");

    await expect(page.getByText(/access denied/i)).toBeVisible();
    await expect(page.getByRole("button", { name: "Add skill" })).not.toBeVisible();
  });

  test("admin can create, edit and delete a skill", async ({ page }) => {
    await login(page, "admin", "admin");
    await page.goto("/admin/skills");

    await expect(page.getByText(/access denied/i)).not.toBeVisible();

    const dialog = page.getByRole("dialog");

    // The empty state repeats the header action.
    await page.getByRole("button", { name: "Add skill" }).first().click();
    const skillName = `e2e-skill-${Date.now()}`;
    await dialog.getByLabel("Name", { exact: true }).fill(skillName);
    await dialog.getByRole("button", { name: "tool", exact: true }).click();
    await dialog.getByRole("button", { name: "Create skill" }).click();
    await expect(dialog).not.toBeVisible();

    const row = page.locator("tr", { hasText: skillName });
    await expect(row).toBeVisible();
    await expect(row.getByText("Tool", { exact: true })).toBeVisible();

    await row.getByRole("button", { name: `Edit ${skillName}`, exact: true }).click();
    const renamed = `${skillName}-renamed`;
    await dialog.getByLabel("Name", { exact: true }).fill(renamed);
    await dialog.getByRole("button", { name: "Update skill" }).click();
    await expect(dialog).not.toBeVisible();

    const renamedRow = page.locator("tr", { hasText: renamed });
    await expect(renamedRow).toBeVisible();
    // the original (pre-rename) name must no longer appear anywhere
    await expect(page.getByText(skillName, { exact: true })).toHaveCount(0);

    await renamedRow.getByRole("button", { name: `Delete ${renamed}`, exact: true }).click();
    const confirmDialog = page.getByRole("alertdialog");
    await expect(confirmDialog).toBeVisible();
    await confirmDialog.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(page.locator("tr", { hasText: renamed })).toHaveCount(0);
  });
});
