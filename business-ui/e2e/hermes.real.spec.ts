import { readFile } from "node:fs/promises";
import { expect, test, type BrowserContext } from "@playwright/test";
import { FEBRUARY_XLSX, JANUARY_XLSX, LOGO_PNG } from "./fixtures/workbooks";
import {
  ADMIN_UI_URL,
  BUSINESS_URL,
  adminApi,
  adminToken,
  allowPackages,
  markVision,
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
 * terminal, which the member sees and downloads, builds a pivot workbook and a
 * deck from two workbooks the member gives it, and builds a web app it checks
 * in headless Chromium and hands over as a ZIP; a provider out of quota ends a
 * turn with a reason the member can act on. The organization's capabilities, model grant and
 * Hermes settings are restored and the approved skill is removed.
 */
const ENABLED = process.env.E2E_HERMES_UI === "true";
const MODEL = process.env.E2E_HERMES_MODEL || "gpt-4o-mini";
const BUSINESS_API = process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
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
    let restoreVision: (() => Promise<void>) | null = null;

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

      // Files the member gives Hermes: two workbooks and a logo into its
      // workspace, a pivot workbook and a deck back, with the pivot's totals
      // from the uploads and the logo's size as Hermes's code read it.
      const workbook = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
      // After the reload the composer takes Hermes from the conversation once it has loaded.
      await expect(member.getByRole("button", { name: "Choose workflow" })).toHaveText(/Hermes Agent/, {
        timeout: 30_000,
      });
      await member.locator('input[type="file"]').setInputFiles([
        { name: "january.xlsx", mimeType: workbook, buffer: Buffer.from(JANUARY_XLSX, "base64") },
        { name: "february.xlsx", mimeType: workbook, buffer: Buffer.from(FEBRUARY_XLSX, "base64") },
        { name: "logo.png", mimeType: "image/png", buffer: Buffer.from(LOGO_PNG, "base64") },
      ]);
      await expect(member.getByRole("button", { name: "Remove logo.png" })).toBeVisible({ timeout: 30_000 });
      await ask(member, "HERMES_INPUTS: build a pivot and a deck", "PIVOT_TOTALS:North=22;South=16");
      // A picture reaches the workspace as a file, which Hermes's code reads.
      await expect(member.getByTestId("assistant-content").last()).toContainText("INPUT_IMAGES:logo.png=64x32");

      // Hermes looks at the logo with vision_analyze: the platform passes the
      // picture only once the organization marks its model as taking images.
      await ask(member, "HERMES_VISION", "HERMES_VISION_DONE");
      await expect(member.getByTestId("assistant-content").last()).toContainText("VISION_UNSEEN");
      restoreVision = await markVision(request, MODEL);
      await ask(member, "HERMES_VISION", "HERMES_VISION_DONE");
      await expect(member.getByTestId("assistant-content").last()).toContainText("VISION_SEEN:1");
      const given = member.getByRole("list", { name: "Your files" }).last();
      await expect(given).toContainText("january.xlsx");
      await expect(given).toContainText("february.xlsx");
      await expect(given).toContainText("logo.png");
      const made = member.getByRole("list", { name: "Attached files" }).last();
      await expect(made).toContainText("pivot.xlsx");
      await expect(made).toContainText("report.pptx");
      for (const [name, entry] of [
        ["pivot.xlsx", "xl/workbook.xml"],
        ["report.pptx", "ppt/slides/slide2.xml"],
      ] as const) {
        const downloading = member.waitForEvent("download");
        await made.getByRole("button", { name: `Download ${name}` }).click();
        const downloaded = await downloading;
        expect(downloaded.suggestedFilename()).toBe(name);
        // Zip entry names are stored as they are in each local header.
        expect((await readFile((await downloaded.path())!)).includes(Buffer.from(entry))).toBe(true);
      }
      await member.reload();
      await expect(member.getByRole("list", { name: "Your files" }).last()).toContainText("february.xlsx", {
        timeout: 30_000,
      });
      await expect(member.getByRole("list", { name: "Attached files" }).last()).toContainText("report.pptx");

      // A web app: Hermes is told about its sandbox, builds the app, installs
      // an npm package through hermes-egress, checks the page in headless
      // Chromium, and hands over a screenshot and the project as a ZIP.
      // After the reload the composer takes Hermes from the conversation once it has loaded.
      await expect(member.getByRole("button", { name: "Choose workflow" })).toHaveText(/Hermes Agent/, {
        timeout: 30_000,
      });
      await ask(member, "HERMES_INSTRUCTIONS_SHOW", "HERMES_INSTRUCTIONS_SEEN");
      // Package installs need the organization's default network mode; the test began with none.
      await allowPackages(request);
      await ask(member, "HERMES_WEBAPP", "HERMES_WEBAPP_DONE");
      const report = member.getByTestId("assistant-content").last();
      await expect(report).toContainText("WEBAPP_TEXT:Hello from the API");
      await expect(report).toContainText("NPM_LEFT_PAD:1.3.0");
      await expect(report).toContainText("NODE:v24.");
      await expect(member.getByRole("img", { name: "Generated image 1" }).last()).toBeVisible({ timeout: 30_000 });
      const project = member.getByRole("list", { name: "Attached files" }).last();
      await expect(project).toContainText("webapp.zip");
      const downloadingProject = member.waitForEvent("download");
      await project.getByRole("button", { name: "Download webapp.zip" }).click();
      const zipped = await readFile((await (await downloadingProject).path())!);
      for (const entry of ["webapp/app.py", "webapp/README.md"]) {
        expect(zipped.includes(Buffer.from(entry)), entry).toBe(true);
      }

      // The member opens the running app at its own origin, in a new tab; the
      // ticket leaves the address bar, and nobody else gets in.
      await ask(member, "HERMES_PREVIEW", "HERMES_PREVIEW_DONE");
      const running = member.getByRole("group", { name: "Running apps" }).last();
      await expect(running).toContainText("Port 8898");
      const opening = memberContext.waitForEvent("page");
      await running.getByRole("button", { name: "Open prototype" }).click();
      const preview = await opening;
      await preview.waitForURL(/^http:\/\/[0-9a-f]{32}\.preview\.localhost:8095\/$/u, { timeout: 30_000 });
      await preview.getByRole("button", { name: "Say hello" }).click({ timeout: 30_000 });
      await expect(preview.locator("#out")).toHaveText("Hello from the API", { timeout: 30_000 });
      const previewOrigin = new URL(preview.url()).origin;
      await preview.close();

      const strangerContext = await browser.newContext();
      contexts.push(strangerContext);
      const stranger = await strangerContext.newPage();
      await stranger.goto(`${previewOrigin}/`, { timeout: 30_000 });
      await expect(stranger.getByRole("heading")).toHaveText("This preview is not available", { timeout: 30_000 });
      // A ticket opens once: the first browser gets in, the next one does not.
      const previewId = new URL(previewOrigin).hostname.split(".")[0].replace(
        /^(.{8})(.{4})(.{4})(.{4})(.{12})$/u,
        "$1-$2-$3-$4-$5"
      );
      const ticketUrl = await member.evaluate(
        async ([api, id]) => {
          const keycloak = (window as Window & { keycloak?: { token?: string } }).keycloak;
          const headers = { Authorization: `Bearer ${keycloak?.token}` };
          // The conversation in use is the member's most recent one.
          const listed = (await (await fetch(`${api}/v1/conversations?limit=1`, { headers })).json()) as {
            data: { id: string }[];
          };
          const response = await fetch(`${api}/v1/conversations/${listed.data[0].id}/previews/${id}/ticket`, {
            method: "POST",
            headers,
          });
          return ((await response.json()) as { url: string }).url;
        },
        [BUSINESS_API, previewId]
      );
      await stranger.goto(ticketUrl, { timeout: 30_000 });
      await expect(stranger.getByRole("button", { name: "Say hello" })).toBeVisible({ timeout: 30_000 });
      const late = await (await browser.newContext()).newPage();
      await late.goto(ticketUrl, { timeout: 30_000 });
      await expect(late.getByRole("heading")).toHaveText("This preview is not available", { timeout: 30_000 });
      await late.context().close();

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
      if (restoreVision) await restoreVision();
      const fresh = await adminToken(request);
      const skills = await adminApi<{ id: string; name: string }[]>(request, fresh, "GET", "/v1/skills/");
      for (const skill of skills.filter((item) => item.name === skillName)) {
        await adminApi(request, fresh, "DELETE", `/v1/skills/${skill.id}`, undefined, 204);
      }
      await restore();
    }
  });
});
