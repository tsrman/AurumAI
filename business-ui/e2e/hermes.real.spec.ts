import { readFile } from "node:fs/promises";
import { expect, test, type BrowserContext } from "@playwright/test";
import {
  ADMIN_UI_URL,
  BUSINESS_URL,
  adminApi,
  adminToken,
  ask as askWithin,
  chooseHermes,
  chooseHermesModel,
  enableHermes,
  hermesWorkflow,
  loginAt,
  section,
} from "./helpers/hermes";

/**
 * Hermes Agent end to end against the deterministic stack
 * (smoke-test-hermes-ui.sh): the gateway scripts Hermes's native tool calls
 * from markers in the user's message. An administrator picks the
 * organization's Hermes model in Admin UI; a member chooses Hermes in the
 * composer and sees its tool cards, also after a reload, where Hermes stays
 * chosen; answers its question card; finds its memory in their settings and
 * clears it; and finds a skill it wrote waiting for review. The administrator
 * reads the draft's files and approves it, and the member's next turn has the
 * skill in the sandbox; Hermes attaches a chart and a CSV it made in its
 * terminal, which the member sees and downloads; a provider out of quota ends
 * a turn with a reason the member can act on. The organization's capabilities, model grant and
 * Hermes settings are restored and the approved skill is removed.
 */
const ENABLED = process.env.E2E_HERMES_UI === "true";
const MODEL = process.env.E2E_HERMES_MODEL || "gpt-4o-mini";
// A sandbox is created, or restored, on the first turn of a conversation.
const TURN_TIMEOUT = 240_000;
const ask = (page: Parameters<typeof askWithin>[0], text: string, expected: string | RegExp) =>
  askWithin(page, text, expected, TURN_TIMEOUT);

// Playwright traces persist request headers, including Keycloak bearer tokens.
test.use({ trace: "off" });

test.describe("Hermes Agent: chat, questions, memory and skill drafts in the browser", () => {
  test.skip(!ENABLED, "E2E_HERMES_UI is not enabled");

  test("a member works with Hermes and an administrator approves the skill it wrote", async ({ browser, request }) => {
    test.setTimeout(1_200_000);
    const token = await adminToken(request);
    const restore = await enableHermes(request, token, MODEL);
    // Drafts are kept per user by their content: a fresh name per run.
    const skillName = `e2e-${Date.now().toString(36)}`;
    const contexts: BrowserContext[] = [];
    let restoreWorkflow: (() => Promise<void>) | null = null;

    try {
      const adminContext = await browser.newContext();
      contexts.push(adminContext);
      const admin = await adminContext.newPage();
      await loginAt(admin, ADMIN_UI_URL, /\/admin\/users$/, "admin");
      await chooseHermesModel(admin, MODEL);

      const memberContext = await browser.newContext();
      contexts.push(memberContext);
      const member = await memberContext.newPage();
      await loginAt(member, BUSINESS_URL, /\/chat$/, "user");
      await chooseHermes(member);

      // A platform tool through the MCP bridge: its card, and the card after a reload.
      await ask(member, "HERMES_MCP:knowledge_search", /\S/u);
      await expect(member.getByLabel(/^Searching knowledge: /).last()).toBeVisible();
      await member.reload();
      await expect(member.getByLabel(/^Searching knowledge: /).last()).toBeVisible({ timeout: 30_000 });
      // The conversation runs on Hermes: it stays chosen after the reload.
      await expect(member.getByRole("button", { name: "Choose workflow" })).toHaveText(/Hermes Agent/, {
        timeout: 30_000,
      });

      // Hermes asks through a question card; the answer reaches it.
      await expect(member.getByLabel("Message")).toBeEnabled({ timeout: TURN_TIMEOUT });
      await member.getByLabel("Message").fill("HERMES_CLARIFY");
      await member.getByLabel("Send").click();
      await expect(member.getByText("Which city should the trip start in?", { exact: true })).toBeVisible({
        timeout: TURN_TIMEOUT,
      });
      await member.getByRole("button", { name: "Rome", exact: true }).click();
      await expect(member.getByTestId("assistant-content").last()).toContainText(/HERMES_CLARIFY_DONE:.*Rome/u, {
        timeout: TURN_TIMEOUT,
      });

      // What Hermes remembers is the member's, in their settings, and can be cleared.
      await ask(member, "HERMES_MEMORY_ADD", "HERMES_MEMORY_ADDED");
      const settings = await memberContext.newPage();
      await settings.goto(`${BUSINESS_URL}/settings`);
      const memory = section(settings, "Hermes Agent memory");
      await expect(memory).toContainText("AURUM_MEMORY_SENTINEL: the trip starts in Rome", { timeout: 30_000 });
      await memory.getByRole("button", { name: "Clear memory" }).click();
      await settings.getByRole("alertdialog").getByRole("button", { name: "Clear memory" }).click();
      await expect(memory).toContainText("Hermes Agent remembers nothing yet.", { timeout: 30_000 });

      // A skill Hermes writes waits for review in the member's settings.
      await ask(member, `HERMES_SKILL_CREATE:${skillName}`, "HERMES_SKILL_CREATED");
      await expect(member.getByLabel(/^Using a skill: /).last()).toBeVisible();
      await settings.reload();
      const skills = section(settings, "Hermes Agent skills");
      await expect(skills.getByRole("listitem").filter({ hasText: skillName })).toContainText("Waiting for review", {
        timeout: 30_000,
      });

      // The administrator reads every file of the draft and approves it.
      await admin.goto(`${ADMIN_UI_URL}/admin/skills`);
      await admin.getByRole("button", { name: `Review the skill ${skillName}` }).click();
      const review = admin.getByRole("dialog", { name: `Review ${skillName}` });
      await expect(review).toContainText("SKILL.md");
      await expect(review).toContainText("AURUM_SKILL_SENTINEL: greet the user.");
      await review.getByRole("button", { name: "Approve" }).click();
      await expect(review).toBeHidden({ timeout: 30_000 });
      await expect(admin.getByRole("row").filter({ hasText: skillName })).toBeVisible({ timeout: 30_000 });

      await settings.reload();
      await expect(section(settings, "Hermes Agent skills").getByRole("listitem").filter({ hasText: skillName }))
        .toContainText("Approved", { timeout: 30_000 });

      // Approved, it is a platform code skill of its author: in the next turn's sandbox.
      await ask(member, `HERMES_SKILLS_LIST:${skillName}`, "HERMES_PLATFORM_SKILL_SEEN");

      // Files Hermes makes in its terminal: the chart under the answer, the
      // CSV offered for download, both still there after a reload; a platform
      // secret in the workspace is refused.
      await ask(member, "HERMES_FILES", "HERMES_FILES_DONE");
      await expect(member.getByTestId("assistant-content").last()).toContainText("file_not_allowed");
      await expect(member.getByRole("img", { name: "Generated image 1" }).last()).toBeVisible({ timeout: 30_000 });
      const attached = member.getByRole("list", { name: "Attached files" }).last();
      await expect(attached).toContainText("e2e-data.csv");
      const downloading = member.waitForEvent("download");
      await attached.getByRole("button", { name: "Download e2e-data.csv" }).click();
      const downloaded = await downloading;
      expect(downloaded.suggestedFilename()).toBe("e2e-data.csv");
      expect(await readFile((await downloaded.path())!, "utf8")).toBe("month,revenue\nJan,10\nFeb,12\n");
      await member.reload();
      await expect(member.getByRole("list", { name: "Attached files" }).last()).toContainText("e2e-data.csv", {
        timeout: 30_000,
      });
      await expect(member.getByRole("img", { name: "Generated image 1" }).last()).toBeVisible({ timeout: 30_000 });

      // An agent definition can be a Hermes Agent, for workflow nodes, on the
      // organization's Hermes model and without tools of its own.
      await admin.goto(`${ADMIN_UI_URL}/admin/agents/new`);
      await admin.getByLabel("Type").selectOption("hermes");
      await expect(admin.getByText(`Runs on the organization's Hermes model: ${MODEL}`)).toBeVisible();
      await expect(admin.getByRole("heading", { name: "Tools" })).toHaveCount(0);

      // A workflow node that a Hermes Agent runs, in this conversation's sandbox.
      const workflow = await hermesWorkflow(request, await adminToken(request), MODEL, "MULTI_AGENT_NODE:build");
      restoreWorkflow = workflow.restore;
      await member.reload();
      await member.getByRole("button", { name: "Choose workflow" }).click();
      await member.getByRole("option", { name: new RegExp(`^${workflow.name}`) }).click();
      await ask(member, "Build it with the workflow", "MULTI_AGENT_FINAL:build HERMES");

      // A provider out of quota ends the turn with a reason the member can act
      // on, and its message, with its address, never reaches the member.
      const replies = member.getByTestId("assistant-content");
      const before = await replies.count();
      await expect(member.getByLabel("Message")).toBeEnabled({ timeout: TURN_TIMEOUT });
      await member.getByLabel("Message").fill("HERMES_PROVIDER_REFUSES");
      await member.getByLabel("Send").click();
      await expect(replies).toHaveCount(before + 1, { timeout: TURN_TIMEOUT });
      await expect(replies.last()).toContainText("out of quota or rate limited", { timeout: TURN_TIMEOUT });
      await expect(replies.last()).toContainText("(hermes_model_unavailable)");
      await expect(replies.last()).not.toContainText("provider.invalid");
      // Set apart from answers, with a way to try again instead of copy and regenerate.
      await expect(member.getByText("The answer could not be completed").last()).toBeVisible();
      await expect(member.getByRole("button", { name: "Try again" }).last()).toBeVisible();
      // The history keeps the reason: it is still there after a reload.
      await member.reload();
      await expect(member.getByTestId("assistant-content").last()).toContainText("out of quota or rate limited", {
        timeout: 30_000,
      });
    } finally {
      for (const context of contexts) await context.close();
      if (restoreWorkflow) await restoreWorkflow();
      const fresh = await adminToken(request);
      const skills = await adminApi<{ id: string; name: string }[]>(request, fresh, "GET", "/v1/skills/");
      for (const skill of skills.filter((item) => item.name === skillName)) {
        await adminApi(request, fresh, "DELETE", `/v1/skills/${skill.id}`, undefined, 204);
      }
      await restore();
    }
  });
});
