import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

test.describe("Settings", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("a settings change is saved on the backend and survives a reload", async ({
    page,
  }) => {
    await page.goto("/settings");

    const timestampsCheckbox = page.getByLabel("Show message times");
    await expect(timestampsCheckbox).toBeVisible();

    const wasChecked = await timestampsCheckbox.isChecked();
    if (wasChecked) {
      await timestampsCheckbox.uncheck();
    } else {
      await timestampsCheckbox.check();
    }

    await page.getByRole("button", { name: /save/i }).click();
    await expect(page.getByText("Settings saved")).toBeVisible();

    await page.reload();
    await expect(page.getByLabel("Show message times")).toBeChecked({
      checked: !wasChecked,
    });

    // Restore original value so the test is idempotent across runs.
    const checkboxAfter = page.getByLabel("Show message times");
    if (wasChecked) {
      await checkboxAfter.check();
    } else {
      await checkboxAfter.uncheck();
    }
    await page.getByRole("button", { name: /save/i }).click();
    await expect(page.getByText("Settings saved")).toBeVisible();
  });

  test("the default model choice is saved on the backend and survives a reload", async ({
    page,
  }) => {
    await page.goto("/settings");

    const modelSelect = page.getByLabel("Default model");
    await expect(modelSelect).toBeVisible();

    // Compare option values, not labels: the organization-default option has
    // a label but the empty value that the select actually reports.
    const values = await modelSelect
      .locator("option")
      .evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value));
    expect(values.length).toBeGreaterThan(1);

    const original = await modelSelect.inputValue();
    const next = values.find((value) => value !== original) ?? values[0];

    await modelSelect.selectOption({ value: next });
    await page.getByRole("button", { name: /save/i }).click();
    await expect(page.getByText("Settings saved")).toBeVisible();

    await page.reload();
    await expect(page.getByLabel("Default model")).toHaveValue(next);

    // Restore original value so the test is idempotent across runs.
    await page.getByLabel("Default model").selectOption({ value: original });
    await page.getByRole("button", { name: /save/i }).click();
    await expect(page.getByText("Settings saved")).toBeVisible();
  });
});
