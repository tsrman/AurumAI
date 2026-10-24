import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";
import { bundleWithoutSkillMd, greetSkillBundle } from "./helpers/skillBundle";

/**
 * /admin/skills for type="code" skills — real end-to-end against the real
 * POST/PUT /v1/skills/code routes (business-backend/src/routers/
 * skill_bundles.py) and real MinIO storage, same "no stubbing" approach as
 * admin-skills.spec.ts. Uploads real .zip bundles built by
 * helpers/skillBundle.ts, not fake bytes, since the backend actually parses
 * them with zipfile.
 */
test.describe("Admin: code skills (bundle upload)", () => {
  test.beforeEach(async ({ page }) => {
    await login(page, "admin", "admin");
    await page.goto("/admin/skills");
    await expect(page.getByText(/access denied/i)).not.toBeVisible();
  });

  test("admin uploads a code skill and sees its name from SKILL.md", async ({ page }) => {
    const skillName = `e2e-code-skill-${Date.now()}`;
    const zip = greetSkillBundle(skillName);

    const dialog = page.getByRole("dialog");
    await page.getByRole("button", { name: "Add skill" }).first().click();
    await dialog.getByLabel("Name", { exact: true }).fill(skillName);
    await dialog.getByRole("button", { name: "code", exact: true }).click();

    await dialog
      .getByLabel("Skill bundle (.zip with SKILL.md)")
      .setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: zip });

    await dialog.getByRole("button", { name: "Create skill" }).click();
    await expect(dialog).not.toBeVisible();

    const row = page.locator("tr", { hasText: skillName });
    await expect(row).toBeVisible();
    await expect(row.getByText("Code", { exact: true })).toBeVisible();
    // "Endpoint or activity" column falls back to bundleSkillName, which
    // comes from the uploaded SKILL.md's frontmatter `name:`, not the
    // display name typed into the form.
    await expect(row.getByText(skillName)).toHaveCount(2);

    await row.getByRole("button", { name: `Delete ${skillName}`, exact: true }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Delete", exact: true }).click();
    await expect(page.locator("tr", { hasText: skillName })).toHaveCount(0);
  });

  test("a bundle without SKILL.md is rejected and no skill is created", async ({ page }) => {
    const skillName = `e2e-bad-bundle-${Date.now()}`;
    const zip = bundleWithoutSkillMd();

    const dialog = page.getByRole("dialog");
    await page.getByRole("button", { name: "Add skill" }).first().click();
    await dialog.getByLabel("Name", { exact: true }).fill(skillName);
    await dialog.getByRole("button", { name: "code", exact: true }).click();
    await dialog
      .getByLabel("Skill bundle (.zip with SKILL.md)")
      .setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: zip });

    await dialog.getByRole("button", { name: "Create skill" }).click();

    // uploadCodeSkill's unwrapUploadError surfaces the real backend
    // validation message (business-backend/src/services/skill_bundle.py),
    // not a generic "HTTP 400".
    await expect(dialog.getByText("Bundle must contain a top-level SKILL.md file")).toBeVisible();
    await expect(dialog).toBeVisible(); // stays open, nothing created
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.locator("tr", { hasText: skillName })).toHaveCount(0);
  });

  test("admin replaces the bundle of an existing code skill", async ({ page }) => {
    const skillName = `e2e-replace-bundle-${Date.now()}`;
    const dialog = page.getByRole("dialog");

    await page.getByRole("button", { name: "Add skill" }).first().click();
    await dialog.getByLabel("Name", { exact: true }).fill(skillName);
    await dialog.getByRole("button", { name: "code", exact: true }).click();
    await dialog
      .getByLabel("Skill bundle (.zip with SKILL.md)")
      .setInputFiles({
        name: "bundle.zip",
        mimeType: "application/zip",
        buffer: greetSkillBundle(`${skillName}-v1`),
      });
    await dialog.getByRole("button", { name: "Create skill" }).click();
    await expect(dialog).not.toBeVisible();

    const row = page.locator("tr", { hasText: skillName });
    await expect(row.getByText(`${skillName}-v1`)).toBeVisible();

    await row.getByRole("button", { name: /^Edit / }).click();
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(`${skillName}-v1`)).toBeVisible();

    const replaceInput = dialog.locator('input[type="file"]');
    await replaceInput.setInputFiles({
      name: "bundle-v2.zip",
      mimeType: "application/zip",
      buffer: greetSkillBundle(`${skillName}-v2`),
    });
    await dialog.getByRole("button", { name: "Replace bundle" }).click();
    await expect(dialog.getByText("Bundle updated.")).toBeVisible();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();

    const updatedRow = page.locator("tr", { hasText: skillName });
    await expect(updatedRow.getByText(`${skillName}-v2`)).toBeVisible();

    await updatedRow.getByRole("button", { name: /^Delete / }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Delete", exact: true }).click();
    await expect(page.locator("tr", { hasText: skillName })).toHaveCount(0);
  });
});
