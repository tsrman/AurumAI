import { readFileSync } from "node:fs";

import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
} from "@playwright/test";


const ENABLED = process.env.E2E_AUTHORIZATION_UI === "true";
const STATE_PATH = process.env.E2E_AUTHORIZATION_UI_STATE || "";
const MARKER = process.env.E2E_AUTHORIZATION_UI_MARKER || "";
const BUSINESS_UI_URL =
  process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const ADMIN_UI_URL = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";

type FixtureResource = { id: string; name: string };
type FixtureState = {
  marker: string;
  model_id: string;
  organization_label: string;
  skill: FixtureResource;
  knowledge_base: FixtureResource;
  agent: FixtureResource;
  workflow: FixtureResource;
};
type ResourceType =
  | "model"
  | "skill"
  | "knowledge_base"
  | "agent_definition"
  | "workflow";

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function fixtureState(): FixtureState {
  const value = JSON.parse(readFileSync(STATE_PATH, "utf8")) as Partial<FixtureState>;
  const resources = [
    value.skill,
    value.knowledge_base,
    value.agent,
    value.workflow,
  ];
  if (
    value.marker !== MARKER ||
    value.model_id !== `${MARKER}-model` ||
    !value.organization_label ||
    resources.some((resource) => !resource?.id || !resource.name)
  ) {
    throw new Error("Authorization UI fixture state is incomplete");
  }
  return value as FixtureState;
}

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

async function freshBusinessPage(
  browser: Browser,
  username: string,
  password: string
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await loginAt(
    page,
    BUSINESS_UI_URL,
    new RegExp(`${escapeRegExp(BUSINESS_UI_URL)}/chat$`),
    username,
    password
  );
  await expect(page.getByLabel("Message")).toBeVisible();
  return { context, page };
}

async function goToNewChat(page: Page): Promise<void> {
  await page
    .getByLabel("Sidebar")
    .getByRole("button", { name: "New chat" })
    .click();
  await expect(page).toHaveURL(`${BUSINESS_UI_URL}/chat`);
  await expect(page.getByLabel("Message")).toBeVisible();
}

async function goToKnowledge(page: Page): Promise<void> {
  await page
    .getByRole("link", { name: "Knowledge bases", exact: true })
    .click();
  await expect(page).toHaveURL(`${BUSINESS_UI_URL}/knowledge`);
}

async function goToAdminPage(
  page: Page,
  linkName: string,
  path: string
): Promise<void> {
  await page.getByRole("link", { name: linkName, exact: true }).click();
  await expect(page).toHaveURL(`${ADMIN_UI_URL}${path}`);
}

async function selectPickerOption(
  page: Page,
  pickerName: string,
  optionName: string
): Promise<void> {
  await page.getByRole("button", { name: pickerName }).click();
  await pickerOption(page, optionName).click();
}

function pickerOption(page: Page, optionName: string): Locator {
  return page.getByRole("option", {
    name: new RegExp(`^${escapeRegExp(optionName)}(?:\\s|$)`),
  });
}

async function selectResource(
  page: Page,
  type: ResourceType,
  id: string
): Promise<Locator> {
  if (new URL(page.url()).pathname !== "/admin/access") {
    await page.getByRole("link", { name: "Access", exact: true }).click();
    await expect(page).toHaveURL(`${ADMIN_UI_URL}/admin/access`);
  }
  await page
    .getByRole("combobox", { name: "Resource type", exact: true })
    .selectOption(type);
  await page
    .getByRole("textbox", { name: "Search resources", exact: true })
    .fill(id);
  const resourcePicker = page.getByRole("combobox", { name: "Resource", exact: true });
  await expect(resourcePicker).toBeVisible();
  await resourcePicker.selectOption(id);
  const editor = page.getByRole("heading", { name: "Resource grants" }).locator("..");
  await expect(editor).toBeVisible();
  await expect(editor.getByText("Loading grants...")).toHaveCount(0);
  await expect(
    editor.getByRole("button", { name: "Save grants" })
  ).toBeEnabled();
  return editor;
}

function analystsCapabilitiesEditor(page: Page): Locator {
  return page
    .getByRole("heading", {
      name: "Capabilities of group analysts",
      exact: true,
    })
    .locator("..");
}

async function saveGrants(page: Page, editor: Locator): Promise<void> {
  const reconciled = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().endsWith("/v1/admin/authorization/grants/reconcile")
  );
  await editor.getByRole("button", { name: "Save grants" }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Apply" })
    .click();
  expect((await reconciled).status()).toBe(200);
  await expect(page.getByRole("alertdialog")).not.toBeVisible();
}

async function addGrant(
  page: Page,
  editor: Locator,
  principalText: string,
  permission: "read" | "write"
): Promise<void> {
  const recipient = editor.getByLabel("Recipient of the new grant");
  const option = recipient.locator("option").filter({ hasText: principalText });
  await expect(option).toHaveCount(1);
  const value = await option.getAttribute("value");
  expect(value).not.toBeNull();
  await recipient.selectOption(value!);
  await editor.getByLabel("Access level of the new grant").selectOption(permission);
  await editor.getByRole("button", { name: "Add grant" }).click();
  await saveGrants(page, editor);
}

async function removeGrant(
  page: Page,
  editor: Locator,
  principalLabel: string
): Promise<void> {
  await editor
    .getByRole("button", {
      name: `Remove grant ${principalLabel}`,
      exact: true,
    })
    .click();
  await saveGrants(page, editor);
}

// Playwright traces persist request headers, including Keycloak bearer
// tokens. This acceptance intentionally keeps only failure screenshots.
test.use({ trace: "off" });

test.describe("Authorization v2: Admin UI to workflow runtime", () => {
  test.skip(!ENABLED, "E2E_AUTHORIZATION_UI is not enabled");

  test("capabilities and five resource grants apply and revoke live", async ({
    browser,
  }) => {
    test.setTimeout(360_000);
    expect(MARKER).toMatch(/^authorization-ui-e2e-[a-f0-9]{10,32}$/);
    expect(STATE_PATH).not.toBe("");
    const state = fixtureState();
    const contexts: BrowserContext[] = [];

    const openBusinessPage = async (username: string, password: string) => {
      const session = await freshBusinessPage(browser, username, password);
      contexts.push(session.context);
      return session.page;
    };

    const expectWorkflowPreflightDenied = async (
      page: Page,
      prompt: string
    ): Promise<void> => {
      await goToNewChat(page);
      await selectPickerOption(page, "Choose model", state.model_id);
      await selectPickerOption(page, "Choose workflow", state.workflow.name);
      const denied = page.waitForResponse(
        (response) =>
          response.url().endsWith("/v1/chat/stream") &&
          response.request().method() === "POST"
      );
      await page.getByLabel("Message").fill(prompt);
      await page.getByLabel("Send").click();
      expect((await denied).status()).toBe(404);
      await expect(page.getByText(/Workflow not found/)).toBeVisible();
    };

    try {
      const adminContext = await browser.newContext();
      const memberAdminContext = await browser.newContext();
      contexts.push(adminContext, memberAdminContext);
      const adminPage = await adminContext.newPage();
      const memberAdminPage = await memberAdminContext.newPage();

      await loginAt(
        adminPage,
        ADMIN_UI_URL,
        new RegExp(`${escapeRegExp(ADMIN_UI_URL)}/admin/agents$`),
        "admin",
        "admin"
      );
      await loginAt(
        memberAdminPage,
        ADMIN_UI_URL,
        new RegExp(`${escapeRegExp(ADMIN_UI_URL)}/admin/agents$`),
        "user",
        "user"
      );
      await goToAdminPage(memberAdminPage, "Access", "/admin/access");
      await expect(memberAdminPage.getByText(/access denied/i)).toBeVisible();

      await goToAdminPage(adminPage, "Groups", "/admin/groups");
      await adminPage
        .getByRole("button", { name: "Configure capabilities analysts" })
        .click();
      const analystsCapabilities = analystsCapabilitiesEditor(adminPage);
      await analystsCapabilities
        .getByRole("checkbox", { name: /workspace\.knowledge\.create/ })
        .check();
      await analystsCapabilities
        .getByRole("button", {
          name: "Save capabilities of group analysts",
        })
        .click();
      await expect(
        analystsCapabilities.getByText("Capabilities saved.")
      ).toBeVisible();

      await addGrant(
        adminPage,
        await selectResource(adminPage, "model", state.model_id),
        "Organization:",
        "read"
      );
      await addGrant(
        adminPage,
        await selectResource(adminPage, "skill", state.skill.id),
        "Group: analysts",
        "read"
      );
      await addGrant(
        adminPage,
        await selectResource(
          adminPage,
          "knowledge_base",
          state.knowledge_base.id
        ),
        "User: Regular User",
        "write"
      );
      await addGrant(
        adminPage,
        await selectResource(adminPage, "agent_definition", state.agent.id),
        "User: Regular User",
        "read"
      );
      await addGrant(
        adminPage,
        await selectResource(adminPage, "workflow", state.workflow.id),
        "Group: analysts",
        "read"
      );

      const skillEditor = await selectResource(
        adminPage,
        "skill",
        state.skill.id
      );
      await skillEditor
        .getByLabel("Recipient of the new grant")
        .selectOption({ label: "User: Regular User" });
      await skillEditor
        .getByRole("button", { name: "Check effective access" })
        .click();
      await expect(skillEditor.getByText("Assigned by a grant")).toBeVisible();

      const knowledgeEditor = await selectResource(
        adminPage,
        "knowledge_base",
        state.knowledge_base.id
      );
      await knowledgeEditor
        .getByLabel("Recipient of the new grant")
        .selectOption({ label: "User: Regular User" });
      await knowledgeEditor
        .getByRole("button", { name: "Check effective access" })
        .click();
      await expect(knowledgeEditor.getByText("Assigned by a grant")).toBeVisible();

      const userPage = await openBusinessPage("user", "user");
      await goToKnowledge(userPage);
      await expect(
        userPage.getByRole("button", { name: "Create knowledge base" })
      ).toBeVisible();
      const knowledgeLink = userPage.getByRole("link", {
        name: new RegExp(escapeRegExp(state.knowledge_base.name)),
      });
      await expect(knowledgeLink).toBeVisible();
      await knowledgeLink.click();
      await expect(
        userPage.getByRole("button", { name: "Delete knowledge base" })
      ).toBeVisible();
      await goToNewChat(userPage);
      await userPage.getByRole("button", { name: "Choose model" }).click();
      await expect(pickerOption(userPage, state.model_id)).toBeVisible();
      await userPage.keyboard.press("Escape");
      await userPage.getByRole("button", { name: "Choose workflow" }).click();
      await expect(pickerOption(userPage, state.workflow.name)).toBeVisible();
      await userPage.keyboard.press("Escape");

      const user2Page = await openBusinessPage("user2", "user2");
      await goToKnowledge(user2Page);
      await expect(
        user2Page.getByRole("button", { name: "Create knowledge base" })
      ).toHaveCount(0);
      await expect(
        user2Page.getByText(state.knowledge_base.name, { exact: true })
      ).toHaveCount(0);
      await goToNewChat(user2Page);
      await user2Page.getByRole("button", { name: "Choose model" }).click();
      await expect(pickerOption(user2Page, state.model_id)).toBeVisible();
      await user2Page.keyboard.press("Escape");
      await user2Page.getByRole("button", { name: "Choose workflow" }).click();
      await expect(pickerOption(user2Page, state.workflow.name)).toHaveCount(0);
      await user2Page.keyboard.press("Escape");

      await goToNewChat(userPage);
      await selectPickerOption(userPage, "Choose model", state.model_id);
      await selectPickerOption(
        userPage,
        "Choose workflow",
        state.workflow.name
      );
      await userPage.getByLabel("Message").fill(`${MARKER}-success`);
      await userPage.getByLabel("Send").click();
      const successful = userPage.getByLabel("Workflow progress").last();
      await expect(successful).toContainText("Workflow completed", {
        timeout: 120_000,
      });
      await expect(userPage.getByTestId("assistant-content").last()).toContainText(
        "MULTI_AGENT_FINAL:authorized-node"
      );

      const repliesBefore = await userPage
        .getByTestId("assistant-content")
        .count();
      await selectPickerOption(
        userPage,
        "Choose workflow",
        state.workflow.name
      );
      await userPage.getByLabel("Message").fill(`${MARKER}-live-skill`);
      await userPage.getByLabel("Send").click();
      const progress = userPage.getByLabel("Workflow progress").last();
      await expect(
        progress
          .getByRole("listitem")
          .filter({ hasText: "Authorization node" })
      ).toContainText("Running", { timeout: 90_000 });
      await removeGrant(
        adminPage,
        await selectResource(adminPage, "skill", state.skill.id),
        "analysts"
      );
      await expect(progress).toContainText("Access to the workflow was revoked", {
        timeout: 120_000,
      });
      await expect(userPage.getByTestId("assistant-content")).toHaveCount(
        repliesBefore
      );

      await addGrant(
        adminPage,
        await selectResource(adminPage, "skill", state.skill.id),
        "Group: analysts",
        "read"
      );
      await removeGrant(
        adminPage,
        await selectResource(adminPage, "agent_definition", state.agent.id),
        "Regular User"
      );
      await expectWorkflowPreflightDenied(
        userPage,
        `${MARKER}-agent-revoked`
      );
      await addGrant(
        adminPage,
        await selectResource(adminPage, "agent_definition", state.agent.id),
        "User: Regular User",
        "read"
      );

      await removeGrant(
        adminPage,
        await selectResource(
          adminPage,
          "knowledge_base",
          state.knowledge_base.id
        ),
        "Regular User"
      );
      const noKnowledgePage = await openBusinessPage("user", "user");
      await goToKnowledge(noKnowledgePage);
      await expect(
        noKnowledgePage.getByText(state.knowledge_base.name, { exact: true })
      ).toHaveCount(0);
      await expectWorkflowPreflightDenied(
        userPage,
        `${MARKER}-knowledge-revoked`
      );
      await addGrant(
        adminPage,
        await selectResource(
          adminPage,
          "knowledge_base",
          state.knowledge_base.id
        ),
        "User: Regular User",
        "write"
      );

      await removeGrant(
        adminPage,
        await selectResource(adminPage, "workflow", state.workflow.id),
        "analysts"
      );
      const noWorkflowPage = await openBusinessPage("user", "user");
      await noWorkflowPage
        .getByRole("button", { name: "Choose workflow" })
        .click();
      await expect(pickerOption(noWorkflowPage, state.workflow.name)).toHaveCount(0);
      await addGrant(
        adminPage,
        await selectResource(adminPage, "workflow", state.workflow.id),
        "Group: analysts",
        "read"
      );

      await removeGrant(
        adminPage,
        await selectResource(adminPage, "model", state.model_id),
        state.organization_label
      );
      const noModelPage = await openBusinessPage("user", "user");
      await noModelPage.getByRole("button", { name: "Choose model" }).click();
      await expect(pickerOption(noModelPage, state.model_id)).toHaveCount(0);
      await noModelPage.keyboard.press("Escape");

      await removeGrant(
        adminPage,
        await selectResource(adminPage, "skill", state.skill.id),
        "analysts"
      );
      await removeGrant(
        adminPage,
        await selectResource(adminPage, "agent_definition", state.agent.id),
        "Regular User"
      );
      await removeGrant(
        adminPage,
        await selectResource(
          adminPage,
          "knowledge_base",
          state.knowledge_base.id
        ),
        "Regular User"
      );
      await removeGrant(
        adminPage,
        await selectResource(adminPage, "workflow", state.workflow.id),
        "analysts"
      );

      await goToAdminPage(adminPage, "Groups", "/admin/groups");
      await adminPage
        .getByRole("button", { name: "Configure capabilities analysts" })
        .click();
      const finalAnalystsCapabilities = analystsCapabilitiesEditor(adminPage);
      await finalAnalystsCapabilities
        .getByRole("checkbox", { name: /workspace\.knowledge\.create/ })
        .uncheck();
      await finalAnalystsCapabilities
        .getByRole("button", {
          name: "Save capabilities of group analysts",
        })
        .click();
      await expect(
        finalAnalystsCapabilities.getByText("Capabilities saved.")
      ).toBeVisible();

      const finalUserPage = await openBusinessPage("user", "user");
      await goToKnowledge(finalUserPage);
      await expect(
        finalUserPage.getByRole("button", { name: "Create knowledge base" })
      ).toHaveCount(0);
    } finally {
      await Promise.allSettled(contexts.map((context) => context.close()));
    }
  });
});
