import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const ENABLED = process.env.E2E_WORKFLOW_UI === "true";
const STATE_PATH = process.env.E2E_WORKFLOW_UI_STATE || "";
const MARKER = process.env.E2E_WORKFLOW_UI_MARKER || "";
const BUSINESS_UI_URL = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const ADMIN_UI_URL = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";
const MODEL = process.env.E2E_WORKFLOW_UI_MODEL || "gpt-4o-mini";
const BARRIER_SECONDS = Number(process.env.TEST_GATEWAY_BARRIER_SECONDS || "8");
const ROOT = path.resolve(__dirname, "../..");
const FIXTURE = path.join(ROOT, "scripts", "workflow_ui_e2e.py");

type FixtureState = {
  marker: string;
  organization_id: string;
  member_id: string;
  agent_ids: Record<"supervisor" | "alpha" | "beta", string>;
};

async function loginAt(
  page: Page,
  appUrl: string,
  landing: RegExp,
  username: string,
  password: string
): Promise<void> {
  await page.goto(`${appUrl}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  await page.waitForURL(landing, { timeout: 30_000 });
}

function runFixture(command: "prepare" | "cleanup"): void {
  execFileSync(
    "python3",
    [FIXTURE, command, "--state", STATE_PATH, "--marker", MARKER],
    {
      cwd: ROOT,
      env: process.env,
      stdio: "inherit",
    }
  );
}

function fixtureState(): FixtureState {
  const value = JSON.parse(readFileSync(STATE_PATH, "utf8")) as Partial<FixtureState>;
  if (
    value.marker !== MARKER ||
    !value.organization_id ||
    !value.member_id ||
    !value.agent_ids?.supervisor ||
    !value.agent_ids.alpha ||
    !value.agent_ids.beta
  ) {
    throw new Error("workflow browser fixture state is incomplete");
  }
  return value as FixtureState;
}

async function selectPickerOption(
  page: Page,
  pickerName: string,
  optionName: string
): Promise<void> {
  await page.getByRole("button", { name: pickerName }).click();
  await page.getByRole("option", { name: new RegExp(optionName) }).click();
}

async function closeContext(context: BrowserContext | undefined): Promise<void> {
  if (context) await context.close();
}

// Playwright traces persist request headers, including Keycloak bearer
// tokens. This acceptance intentionally keeps only failure screenshots.
test.use({ trace: "off" });

test.describe("Workflow: end-to-end Admin UI → Business UI", () => {
  test.skip(!ENABLED, "E2E_WORKFLOW_UI is not set; test skipped");

  test("create, grant, run, cancel and archive", async ({ browser }) => {
    test.setTimeout(180_000);

    expect(STATE_PATH, "E2E_WORKFLOW_UI_STATE must point to a private state file").not.toBe("");
    expect(MARKER, "E2E_WORKFLOW_UI_MARKER must be an owned marker").toMatch(
      /^workflow-ui-e2e-[a-f0-9]{10,32}$/
    );

    let adminContext: BrowserContext | undefined;
    let memberContext: BrowserContext | undefined;
    let primaryError: unknown;
    let cleanupRequired = false;

    try {
      cleanupRequired = true;
      runFixture("prepare");
      const state = fixtureState();

      adminContext = await browser.newContext();
      memberContext = await browser.newContext();
      const adminPage = await adminContext.newPage();
      const memberPage = await memberContext.newPage();

      await loginAt(
        adminPage,
        ADMIN_UI_URL,
        new RegExp(`${ADMIN_UI_URL.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/admin/users$`),
        "admin",
        "admin"
      );
      await expect(adminPage.getByRole("link", { name: "Agents", exact: true })).toBeVisible();
      await loginAt(
        memberPage,
        BUSINESS_UI_URL,
        new RegExp(`${BUSINESS_UI_URL.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/chat$`),
        "user",
        "user"
      );
      await expect(memberPage.getByLabel("Message")).toBeVisible();

      const originalName = `${MARKER}-original`;
      const renamedName = `${MARKER}-renamed`;
      await adminPage.goto(`${ADMIN_UI_URL}/admin/workflows`);
      await expect(
        adminPage.getByRole("heading", { name: "Workflows", exact: true })
      ).toBeVisible();

      await adminPage.getByRole("button", { name: "Create workflow", exact: true }).click();
      await adminPage.getByLabel("Workflow name").fill(originalName);
      await adminPage.getByLabel("Workflow description").fill("Original browser workflow");
      await adminPage.getByRole("button", { name: "2. Agents", exact: true }).click();
      await adminPage.getByLabel("Supervisor").selectOption(state.agent_ids.supervisor);
      await adminPage.getByRole("button", { name: "3. Steps", exact: true }).click();
      await adminPage.getByText("Advanced step settings", { exact: true }).click();
      await adminPage.getByLabel("Node 1 key").fill("alpha");
      await adminPage.getByLabel("Node 1 name").fill("Alpha");
      await adminPage.getByLabel("Node 1 agent").selectOption(state.agent_ids.alpha);
      await adminPage
        .getByLabel("Node 1 instruction")
        .fill("MULTI_AGENT_NODE:alpha MULTI_AGENT_BARRIER:workflow-ui");
      await adminPage.getByRole("button", { name: "Add node" }).click();
      await adminPage.getByText("Advanced step settings", { exact: true }).click();
      await adminPage.getByLabel("Node 2 key").fill("beta");
      await adminPage.getByLabel("Node 2 name").fill("Beta");
      await adminPage.getByLabel("Node 2 agent").selectOption(state.agent_ids.beta);
      await adminPage.getByLabel("Node 2 instruction").fill("MULTI_AGENT_NODE:beta");
      await adminPage.getByLabel("Output handle — node Alpha").click();
      await adminPage.getByLabel("Input handle — node Beta").click();
      await adminPage
        .getByRole("combobox", { name: "Link condition alpha → beta", exact: true })
        .selectOption("on_success");

      await adminPage.getByRole("button", { name: "4. Check", exact: true }).click();
      await adminPage.getByRole("button", { name: "Validate" }).click();
      await expect(adminPage.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
      await adminPage.getByRole("button", { name: "Save", exact: true }).click();

      await adminPage.getByRole("button", { name: "Back to workflows", exact: true }).click();
      const originalRow = adminPage.locator("tr", { hasText: originalName });
      await expect(originalRow).toBeVisible();
      await expect(originalRow.locator("td").nth(1)).toHaveText("1");
      await expect(originalRow.locator("td").nth(2)).toHaveText("Active");

      await originalRow.getByRole("button", { name: "Edit", exact: true }).click();
      await expect(
        adminPage.getByRole("heading", { name: "Edit workflow" })
      ).toBeVisible();
      await adminPage.getByLabel("Workflow name").fill(renamedName);
      await adminPage.getByLabel("Workflow description").fill("Renamed browser workflow");
      await adminPage.getByRole("button", { name: "4. Check", exact: true }).click();
      await adminPage.getByRole("button", { name: "Validate" }).click();
      await expect(adminPage.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
      await adminPage.getByRole("button", { name: "Save", exact: true }).click();

      await adminPage.getByRole("button", { name: "Back to workflows", exact: true }).click();
      const renamedRow = adminPage.locator("tr", { hasText: renamedName });
      await expect(renamedRow).toBeVisible();
      await expect(renamedRow.locator("td").nth(1)).toHaveText("2");
      await expect(adminPage.getByText(originalName, { exact: true })).toHaveCount(0);

      await memberPage.reload();
      await expect(memberPage.getByLabel("Message")).toBeVisible();
      await memberPage.getByRole("button", { name: "Choose workflow" }).click();
      await expect(memberPage.getByRole("option", { name: renamedName })).toHaveCount(0);
      await memberPage.keyboard.press("Escape");

      await renamedRow.getByRole("button", { name: "Access", exact: true }).click();
      const grantEditor = adminPage.getByRole("dialog", { name: `Access to “${renamedName}”` });
      const recipient = grantEditor.getByLabel("Recipient of the new grant");
      await expect(
        recipient.locator("option", { hasText: "User: Regular User" })
      ).toHaveCount(1);
      await recipient.selectOption({ label: "User: Regular User" });
      await grantEditor.getByLabel("Access level of the new grant").selectOption("read");
      await grantEditor.getByRole("button", { name: "Add grant" }).click();
      await grantEditor.getByRole("button", { name: "Save grants" }).click();
      const confirm = adminPage.getByRole("alertdialog");
      await expect(confirm).toBeVisible();
      await confirm.getByRole("button", { name: "Apply", exact: true }).click();
      await expect(confirm).not.toBeVisible();
      await expect(grantEditor.getByText("Regular User", { exact: true })).toBeVisible();
      await grantEditor.getByRole("button", { name: "Close", exact: true }).click();
      await expect(grantEditor).not.toBeVisible();

      await memberPage.reload();
      await expect(memberPage.getByLabel("Message")).toBeVisible();
      await selectPickerOption(memberPage, "Choose model", MODEL);
      await selectPickerOption(memberPage, "Choose workflow", renamedName);
      const assistantContents = memberPage.getByTestId("assistant-content");
      const successfulRepliesBefore = await assistantContents.count();
      await memberPage.getByLabel("Message").fill(MARKER);
      await memberPage.getByLabel("Send").click();

      const successfulProgress = memberPage.getByLabel("Workflow progress").last();
      await expect(successfulProgress).toBeVisible({ timeout: 90_000 });
      await expect(successfulProgress.getByRole("listitem").filter({ hasText: "Alpha" })).toContainText(
        "Done",
        { timeout: 120_000 }
      );
      await expect(successfulProgress.getByRole("listitem").filter({ hasText: "Beta" })).toContainText(
        "Done",
        { timeout: 120_000 }
      );
      await expect(successfulProgress).toContainText("Workflow completed", {
        timeout: 120_000,
      });
      await expect(assistantContents).toHaveCount(successfulRepliesBefore + 1, {
        timeout: 120_000,
      });
      await expect(assistantContents.last()).toContainText("MULTI_AGENT_FINAL:alpha,beta");
      await expect(memberPage.getByRole("button", { name: "Choose workflow" })).toContainText(
        "Plain chat"
      );

      const successfulReplyCount = await assistantContents.count();
      await selectPickerOption(memberPage, "Choose workflow", renamedName);
      await memberPage.getByLabel("Message").fill(MARKER);
      await memberPage.getByLabel("Send").click();
      const cancelledProgress = memberPage.getByLabel("Workflow progress").last();
      await expect(
        cancelledProgress.getByRole("listitem").filter({ hasText: "Alpha" })
      ).toContainText("Running", { timeout: 90_000 });
      const cancelAcknowledged = memberPage.waitForResponse(
        (response) =>
          /\/v1\/workflow-runs\/[^/]+\/cancel$/.test(response.url()) &&
          response.request().method() === "POST" &&
          response.status() === 202
      );
      await memberPage.getByLabel("Stop generating").click();
      await cancelAcknowledged;
      await expect(cancelledProgress).toContainText("Workflow cancelled", {
        timeout: 90_000,
      });
      await expect(memberPage.getByLabel("Send")).toBeVisible();
      // A negative assertion must span the deterministic gateway boundary:
      // otherwise a late synthesis answer could arrive after an immediate pass.
      await memberPage.waitForTimeout((BARRIER_SECONDS + 1) * 1000);
      await expect(assistantContents).toHaveCount(successfulReplyCount);

      await renamedRow.getByRole("button", { name: "Archive", exact: true }).click();
      await adminPage
        .getByRole("alertdialog")
        .getByRole("button", { name: "Archive workflow", exact: true })
        .click();
      await expect(renamedRow.locator("td").nth(2)).toHaveText("Archived");
      await memberPage.reload();
      await expect(memberPage.getByLabel("Message")).toBeVisible();
      await memberPage.getByRole("button", { name: "Choose workflow" }).click();
      await expect(memberPage.getByRole("option", { name: renamedName })).toHaveCount(0);
    } catch (error) {
      primaryError = error;
      throw error;
    } finally {
      await Promise.allSettled([closeContext(adminContext), closeContext(memberContext)]);
      if (cleanupRequired) {
        try {
          runFixture("cleanup");
        } catch (cleanupError) {
          if (!primaryError) throw cleanupError;
          console.error("workflow browser fixture cleanup failed after the primary test failure");
        }
      }
    }
  });
});
