import { APIRequestContext, expect, test } from "@playwright/test";
import { login } from "./helpers/auth";

const KEYCLOAK_URL = process.env.KEYCLOAK_URL || "http://localhost:8080";
const ADMIN_URL = process.env.ADMIN_BASE_URL || "http://localhost:8001";

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
  return (await response.json()).access_token as string;
}

test.describe("Authorization v2: admin UI", () => {
  test("capability and grant editors are available only to an administrator", async ({
    page,
    request,
  }) => {
    await login(page, "user", "user");
    await page.goto("/admin/groups");
    await expect(page.getByText(/access denied/i)).toBeVisible();
    await expect(page.getByText("Default permissions")).not.toBeVisible();

    const adminPage = await page.context().browser()?.newPage();
    expect(adminPage).toBeDefined();
    if (!adminPage) return;
    await login(adminPage, "admin", "admin");
    await adminPage.goto("/admin/groups");
    await adminPage.getByRole("button", { name: /Default permissions/ }).click();
    await expect(
      adminPage.getByRole("heading", { name: "Organization capabilities" })
    ).toBeVisible();
    await expect(adminPage.getByLabel(/Permissions/)).toHaveCount(0);
    await adminPage.getByRole("button", { name: "Close" }).click();
    await adminPage.getByRole("button", { name: "Edit group analysts", exact: true }).click();
    const groupDialog = adminPage.getByRole("dialog", { name: "Group “analysts”" });
    await groupDialog.getByRole("tab", { name: "Permissions", exact: true }).click();
    await expect(
      groupDialog.getByRole("heading", { name: "Capabilities of group analysts" })
    ).toBeVisible();
    await groupDialog.getByRole("button", { name: "Close" }).click();

    const token = await adminToken(request);
    const marker = `authorization-v2-ui-${Date.now()}`;
    const created = await request.post(`${ADMIN_URL}/v1/skills/`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { name: marker, type: "tool" },
    });
    expect(created.status()).toBe(201);
    const skillId = ((await created.json()) as { data: { id: string } }).data.id;

    try {
      await adminPage.goto("/admin/skills");
      const row = adminPage.locator("tr", { hasText: marker });
      await expect(row).toBeVisible();
      await row.getByRole("button", { name: `Access to ${marker}`, exact: true }).click();
      const dialog = adminPage.getByRole("dialog", { name: `Access to “${marker}”` });
      await expect(dialog.getByRole("heading", { name: "Who has access" })).toBeVisible();
      await dialog
        .getByLabel("Recipient of the new grant")
        .selectOption({ label: "User: Regular User" });
      await dialog.getByRole("button", { name: "Add grant" }).click();
      await expect(dialog.getByLabel("Access level of Regular User")).toHaveValue("read");
      await dialog.getByRole("button", { name: "Save grants" }).click();
      const confirmation = adminPage.getByRole("alertdialog");
      await expect(confirmation.getByText(/the change applies immediately/i)).toBeVisible();
      await confirmation.getByRole("button", { name: "Apply", exact: true }).click();
      await dialog.getByRole("button", { name: "Check effective access" }).click();
      await expect(dialog.getByText("Assigned by a grant")).toBeVisible();
    } finally {
      const deleted = await request.delete(`${ADMIN_URL}/v1/skills/${skillId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect([204, 404]).toContain(deleted.status());
      await adminPage.close();
    }
  });
});
