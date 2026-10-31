import { createHash, randomBytes } from "node:crypto";
import { expect, test, type BrowserContext } from "@playwright/test";
import { FEBRUARY_XLSX, JANUARY_XLSX, LOGO_PNG } from "./fixtures/workbooks";
import {
  ADMIN_UI_URL,
  BUSINESS_URL,
  TURN_FAILED,
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
 * Hermes Agent with a real model (smoke-test-hermes-ui.sh with
 * HERMES_E2E_REAL_LLM=true): nothing is scripted, so every check rests on an
 * effect the model cannot produce without the tool. It hashes a random value
 * in its terminal (the test computes the same hash), attaches a chart and a
 * CSV it made with matplotlib, builds a pivot workbook and a deck from two
 * workbooks the member gives it (its answer is the pivot's total), builds and
 * checks a Flask prototype it hands over as a ZIP with a screenshot, asks
 * through a question card and uses the answer, saves a random value to the
 * member's memory, and
 * writes a skill that waits for review. Opt-in with E2E_USE_REAL_LLM=true; the
 * model (E2E_HERMES_MODEL) needs native tool calling. The organization's
 * settings are restored, the memory is cleared and the draft withdrawn.
 */
const ENABLED = process.env.E2E_USE_REAL_LLM === "true";
const MODEL = process.env.E2E_HERMES_MODEL || "deepseek-flash-free";
// With a model that takes images (E2E_HERMES_VISION=true), Hermes also looks at a picture.
const VISION = process.env.E2E_HERMES_VISION === "true";
const BUSINESS_API = process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
// A real model reasons over Hermes's large prompt, on top of the sandbox's start.
const TURN_TIMEOUT = 300_000;
const ask = (page: Parameters<typeof askWithin>[0], text: string, expected: string | RegExp) =>
  askWithin(page, text, expected, TURN_TIMEOUT);

// Playwright traces persist request headers, including Keycloak bearer tokens.
test.use({ trace: "off" });

test.describe("Hermes Agent with a real model", () => {
  test.skip(!ENABLED, "E2E_USE_REAL_LLM is not set; test skipped");

  test(`Hermes on ${MODEL} uses its terminal, questions, memory and skills`, async ({ browser, request }) => {
    test.setTimeout(1_800_000);
    const token = await adminToken(request);
    const restore = await enableHermes(request, token, MODEL);
    const nonce = randomBytes(8).toString("hex");
    const skillName = `e2e-${nonce.slice(0, 8)}`;
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
      // A run that failed before its cleanup leaves memory a model would work on.
      await member.evaluate(async (api) => {
        const keycloak = (window as Window & { keycloak?: { token?: string } }).keycloak;
        await fetch(`${api}/v1/hermes/memory`, { method: "DELETE", headers: { Authorization: `Bearer ${keycloak?.token}` } });
      }, BUSINESS_API);
      await chooseHermes(member);

      // The terminal: a hash of a random value only a command can produce.
      const digest = createHash("sha256").update(nonce).digest("hex").slice(0, 16);
      await ask(
        member,
        "Use your terminal to run this exact command and reply with only its output: " +
          `python3 -c "import hashlib; print(hashlib.sha256(b'${nonce}').hexdigest()[:16])"`,
        digest
      );
      await expect(member.getByLabel(/^(Running commands|Running code): /).first()).toBeVisible();
      await member.reload();
      await expect(member.getByRole("button", { name: "Choose workflow" })).toHaveText(/Hermes Agent/, {
        timeout: 30_000,
      });

      // A chart and its data the model makes with matplotlib and attaches.
      await ask(
        member,
        "Use your terminal and Python's matplotlib to draw a bar chart of these monthly sales: Jan 10, Feb 12, " +
          "Mar 7. Save it as /workspace/sales.png and the data as /workspace/sales.csv, then give me both files " +
          "with your attach_file tool. Reply with one short sentence.",
        /\S/u
      );
      await expect(member.getByRole("img", { name: "Generated image 1" }).last()).toBeVisible({ timeout: 30_000 });
      await expect(member.getByRole("list", { name: "Attached files" }).last()).toContainText("sales.csv");

      // The member's workbooks: a pivot workbook and a deck the model builds from them.
      const workbook = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
      await member.locator('input[type="file"]').setInputFiles([
        { name: "january.xlsx", mimeType: workbook, buffer: Buffer.from(JANUARY_XLSX, "base64") },
        { name: "february.xlsx", mimeType: workbook, buffer: Buffer.from(FEBRUARY_XLSX, "base64") },
      ]);
      await expect(member.getByRole("button", { name: "Remove february.xlsx" })).toBeVisible({ timeout: 30_000 });
      await ask(
        member,
        "Using pandas in your terminal, build a pivot table of sales by region and month from the two workbooks I " +
          "attached, save it as /workspace/pivot.xlsx, and make a PowerPoint deck /workspace/sales.pptx with a bar " +
          "chart of it (python-pptx and matplotlib are installed). Give me both files with your attach_file tool, " +
          "then reply with only the total sales of the North region as a number.",
        /\b22\b/u
      );
      const made = member.getByRole("list", { name: "Attached files" }).last();
      await expect(made).toContainText(".xlsx");
      await expect(made).toContainText(".pptx");

      // A web app prototype the model builds, checks in headless Chromium and hands over;
      // Flask comes from the package index, which the default network mode allows.
      await allowPackages(request);
      await ask(
        member,
        "Build a small Flask web app with one page that shows a list of three tasks and a form to add one. " +
          "Test it with pytest, then start it and check the page in headless Chromium with Playwright, adding " +
          "a task. Give me a screenshot of the page with the added task and the project as one ZIP with a " +
          "README.md on how to run it. Reply in one short sentence.",
        /\S/u
      );
      await expect(member.getByRole("img", { name: "Generated image 1" }).last()).toBeVisible({ timeout: 30_000 });
      await expect(member.getByRole("list", { name: "Attached files" }).last()).toContainText(".zip");

      // A picture the model looks at with vision_analyze, once its model is marked as taking images.
      if (VISION) {
        restoreVision = await markVision(request, MODEL);
        await member.locator('input[type="file"]').setInputFiles([
          { name: "logo.png", mimeType: "image/png", buffer: Buffer.from(LOGO_PNG, "base64") },
        ]);
        await expect(member.getByRole("button", { name: "Remove logo.png" })).toBeVisible({ timeout: 30_000 });
        await ask(
          member,
          "Look at the picture I attached with your vision_analyze tool. What single colour fills it? " +
            "Reply with one word.",
          /yellow|gold|orange|amber|ochre|mustard/iu
        );
      }

      // A question card the model asks through clarify, and the answer it uses.
      await expect(member.getByLabel("Message")).toBeEnabled({ timeout: TURN_TIMEOUT });
      await member.getByLabel("Message").fill(
        "Before you answer, use your clarify tool to ask me which city I prefer, offering exactly two " +
          "choices: Paris and Rome. After I answer, reply with one short sentence naming the city I chose."
      );
      await member.getByLabel("Send").click();
      const rome = member.getByRole("button", { name: /^Rome/ });
      const failed = member.getByTestId("assistant-content").filter({ hasText: TURN_FAILED });
      await expect(rome.or(failed).first()).toBeVisible({ timeout: TURN_TIMEOUT });
      await expect(failed, "the turn failed").toHaveCount(0);
      await rome.first().click();
      await expect(member.getByTestId("assistant-content").last()).toContainText("Rome", { timeout: TURN_TIMEOUT });

      // Memory: a random value saved with the memory tool reaches the member's settings.
      const colour = `colour-${nonce.slice(8)}`;
      await ask(
        member,
        `Save this fact to your persistent memory with your memory tool: my favourite colour code is ${colour}. ` +
          "Then confirm in one short sentence.",
        /\S/u
      );
      const settings = await memberContext.newPage();
      await settings.goto(`${BUSINESS_URL}/settings`);
      const memory = section(settings, "Hermes Agent memory");
      await expect(memory).toContainText(colour, { timeout: 30_000 });

      // A skill the model writes waits for review.
      await ask(
        member,
        `Create a new skill with your skill_manage tool: name ${skillName}, category e2e, and a SKILL.md whose ` +
          `frontmatter has name ${skillName} and description "Says hello for the e2e test" and whose body is ` +
          '"Reply with hello." Then confirm in one short sentence.',
        /\S/u
      );
      await settings.reload();
      const draft = section(settings, "Hermes Agent skills").getByRole("listitem").filter({ hasText: skillName });
      await expect(draft).toContainText("Waiting for review", { timeout: 30_000 });

      // A workflow node run by Hermes: its terminal's hash reaches the final answer.
      const nodeNonce = randomBytes(8).toString("hex");
      const nodeDigest = createHash("sha256").update(nodeNonce).digest("hex").slice(0, 16);
      const workflow = await hermesWorkflow(
        request,
        await adminToken(request),
        MODEL,
        "Use your terminal to run this exact command and report only its output: " +
          `python3 -c "import hashlib; print(hashlib.sha256(b'${nodeNonce}').hexdigest()[:16])"`
      );
      restoreWorkflow = workflow.restore;
      // The composer lists the workflows it loaded: this one was granted since.
      await member.reload();
      await member.getByRole("button", { name: "Choose workflow" }).click();
      await member.getByRole("option", { name: new RegExp(`^${workflow.name}`) }).click();
      await ask(member, "Run the workflow and give me the node's output exactly.", nodeDigest);

      // Leave nothing behind: the memory cleared and the draft withdrawn.
      await draft.getByRole("button", { name: `Withdraw the skill ${skillName}` }).click();
      await expect(draft).toHaveCount(0, { timeout: 30_000 });
      await section(settings, "Hermes Agent memory").getByRole("button", { name: "Clear memory" }).click();
      await settings.getByRole("alertdialog").getByRole("button", { name: "Clear memory" }).click();
      await expect(section(settings, "Hermes Agent memory")).toContainText("Hermes Agent remembers nothing yet.", {
        timeout: 30_000,
      });
    } finally {
      for (const context of contexts) await context.close();
      if (restoreWorkflow) await restoreWorkflow();
      if (restoreVision) await restoreVision();
      await restore();
    }
  });
});
