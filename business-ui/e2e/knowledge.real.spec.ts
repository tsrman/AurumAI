import { execFileSync } from "node:child_process";
import path from "node:path";
import { BrowserContext, Page, expect, test } from "@playwright/test";

const ENABLED = process.env.E2E_KNOWLEDGE_UI === "true";
const MARKER = process.env.E2E_KNOWLEDGE_UI_MARKER || "";
const STATE_PATH = process.env.E2E_KNOWLEDGE_UI_STATE || "";
const BUSINESS_UI_URL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000";
const ADMIN_UI_URL = process.env.ADMIN_UI_URL || "http://localhost:3100";
const REPO_ROOT = path.resolve(process.cwd(), "..");
const FIXTURE = path.join(REPO_ROOT, "scripts", "knowledge_ui_e2e.py");

function fixture(command: "capture" | "cleanup", args: string[] = []): void {
  execFileSync(
    process.env.E2E_PYTHON || "python3",
    [FIXTURE, command, "--state", STATE_PATH, "--marker", MARKER, ...args],
    { cwd: REPO_ROOT, stdio: "inherit", env: process.env }
  );
}

async function loginAt(
  page: Page,
  appUrl: string,
  username: string,
  password: string,
  landing: RegExp
): Promise<void> {
  await page.goto(`${appUrl}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  await page.waitForURL(landing, { timeout: 30_000 });
}

test.use({ trace: "off" });

test.describe("Knowledge/RAG: full UI lifecycle", () => {
  test.skip(!ENABLED, "E2E_KNOWLEDGE_UI is not set; test skipped");

  test("grant, create, upload, index, search and delete", async ({ browser }) => {
    test.setTimeout(300_000);
    expect(MARKER).toMatch(/^knowledge-ui-e2e-[a-f0-9]{10,32}$/);
    expect(STATE_PATH).not.toBe("");

    let memberContext: BrowserContext | null = null;
    let adminContext: BrowserContext | null = null;
    let browserFailure: unknown = null;
    const baseName = `${MARKER}-base`;
    const sourceFilename = `${MARKER}.txt`;
    const indexedPhrase = `${MARKER} indexed evidence`;
    const searchPhrase = `${MARKER} searchable phrase`;

    try {
      memberContext = await browser.newContext();
      adminContext = await browser.newContext();
      const memberPage = await memberContext.newPage();
      const adminPage = await adminContext.newPage();

      await loginAt(memberPage, BUSINESS_UI_URL, "user", "user", /\/chat$/);
      await memberPage.goto(`${BUSINESS_UI_URL}/knowledge`);
      await expect(
        memberPage.getByRole("button", { name: "Create knowledge base" })
      ).toHaveCount(0);

      await loginAt(adminPage, ADMIN_UI_URL, "admin", "admin", /\/admin\/agents$/);
      await adminPage.goto(`${ADMIN_UI_URL}/admin/groups`);
      const capability = adminPage.getByRole("checkbox", {
        name: /workspace\.knowledge\.create/,
      });
      await expect(capability).toBeVisible();
      await capability.check();
      const capabilityResponsePromise = adminPage.waitForResponse(
        (response) =>
          response.request().method() === "PUT" &&
          new URL(response.url()).pathname ===
            "/v1/admin/authorization/capabilities/organization"
      );
      await adminPage
        .getByRole("button", { name: "Save organization capabilities" })
        .click();
      const capabilityResponse = await capabilityResponsePromise;
      expect(capabilityResponse.ok()).toBeTruthy();

      await memberPage.reload();
      await expect(
        memberPage.getByRole("button", { name: "Create knowledge base" })
      ).toBeVisible();
      await memberPage
        .getByRole("button", { name: "Create knowledge base" })
        .click();
      await memberPage.getByLabel("Knowledge base name").fill(baseName);
      await memberPage
        .getByLabel("Knowledge base description")
        .fill("Browser-owned Knowledge/RAG fixture");
      await memberPage.getByRole("button", { name: "Create", exact: true }).click();
      await memberPage.waitForURL(/\/knowledge\/[0-9a-f-]{36}$/);
      const knowledgeBaseId = new URL(memberPage.url()).pathname.split("/").pop() || "";
      expect(knowledgeBaseId).toMatch(/^[0-9a-f-]{36}$/);

      let uploadRequests = 0;
      const observeUpload = (request: { method(): string; url(): string }) => {
        if (
          request.method() === "POST" &&
          new URL(request.url()).pathname === "/v1/documents/upload"
        ) {
          uploadRequests += 1;
        }
      };
      memberPage.on("request", observeUpload);
      await memberPage.getByLabel("Choose files").setInputFiles({
        name: `${MARKER}.exe`,
        mimeType: "application/octet-stream",
        buffer: Buffer.from("unsupported"),
      });
      await expect(
        memberPage.getByRole("alert").filter({ hasText: "Not supported" })
      ).toContainText("Not supported");
      await memberPage.waitForTimeout(1_000);
      expect(uploadRequests).toBe(0);
      memberPage.off("request", observeUpload);

      const uploadResponsePromise = memberPage.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          new URL(response.url()).pathname === "/v1/documents/upload"
      );
      await memberPage.getByLabel("Choose files").setInputFiles({
        name: sourceFilename,
        mimeType: "text/plain",
        buffer: Buffer.from(`${indexedPhrase}\n\n${searchPhrase}\n`, "utf8"),
      });
      const uploadResponse = await uploadResponsePromise;
      expect(uploadResponse.ok()).toBeTruthy();
      const uploadBody = (await uploadResponse.json()) as {
        data: { id: string };
      };
      const documentId = uploadBody.data.id;
      expect(documentId).toMatch(/^[0-9a-f-]{36}$/);
      fixture("capture", [
        "--knowledge-base-id",
        knowledgeBaseId,
        "--document-id",
        documentId,
      ]);

      let documentRow = memberPage.getByRole("row", { name: new RegExp(sourceFilename) });
      await expect(documentRow.getByText("Indexed")).toBeVisible({ timeout: 180_000 });
      await expect
        .poll(async () => Number((await documentRow.locator("td").nth(3).textContent())?.trim()))
        .toBeGreaterThan(0);

      await memberPage.reload();
      documentRow = memberPage.getByRole("row", { name: new RegExp(sourceFilename) });
      await expect(documentRow).toBeVisible();
      await memberPage.getByLabel("Search the knowledge base").fill(searchPhrase);
      await memberPage.getByRole("button", { name: "Search", exact: true }).click();
      const results = memberPage.getByRole("list", { name: "Search results" });
      await expect(results).toContainText(searchPhrase, { timeout: 60_000 });
      await expect(results).toContainText(sourceFilename);

      await memberPage
        .getByRole("button", { name: `Delete document ${sourceFilename}` })
        .click();
      await memberPage.getByRole("alertdialog").getByRole("button", { name: "Delete", exact: true }).click();
      await expect(documentRow).toHaveCount(0);
      const deleteBase = memberPage.getByRole("button", { name: "Delete knowledge base" });
      await expect(deleteBase).toBeEnabled();
      await deleteBase.click();
      await memberPage.getByRole("alertdialog").getByRole("button", { name: "Delete", exact: true }).click();
      await memberPage.waitForURL(`${BUSINESS_UI_URL}/knowledge`);
      await expect(memberPage.getByRole("link", { name: new RegExp(baseName) })).toHaveCount(0);
    } catch (error) {
      browserFailure = error;
      throw error;
    } finally {
      await memberContext?.close();
      await adminContext?.close();
      try {
        fixture("cleanup");
      } catch (cleanupError) {
        console.error("Knowledge/RAG fixture cleanup failed; wrapper will retry it.");
        if (!browserFailure) throw cleanupError;
      }
    }
  });
});
