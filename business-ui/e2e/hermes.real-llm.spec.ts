import { createHash, randomBytes } from "node:crypto";
import { expect, test, type BrowserContext } from "@playwright/test";
import {
  ADMIN_UI_URL,
  BUSINESS_URL,
  adminToken,
  ask as askWithin,
  chooseHermes,
  chooseHermesModel,
  enableHermes,
  loginAt,
  section,
} from "./helpers/hermes";

/**
 * Hermes Agent with a real model (smoke-test-hermes-ui.sh with
 * HERMES_E2E_REAL_LLM=true): nothing is scripted, so every check rests on an
 * effect the model cannot produce without the tool. It hashes a random value
 * in its terminal (the test computes the same hash), asks through a question
 * card and uses the answer, saves a random value to the member's memory, and
 * writes a skill that waits for review. Opt-in with E2E_USE_REAL_LLM=true; the
 * model (E2E_HERMES_MODEL) needs native tool calling. The organization's
 * settings are restored, the memory is cleared and the draft withdrawn.
 */
const ENABLED = process.env.E2E_USE_REAL_LLM === "true";
const MODEL = process.env.E2E_HERMES_MODEL || "deepseek-flash-free";
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

      // A question card the model asks through clarify, and the answer it uses.
      await expect(member.getByLabel("Message")).toBeEnabled({ timeout: TURN_TIMEOUT });
      await member.getByLabel("Message").fill(
        "Before you answer, use your clarify tool to ask me which city I prefer, offering exactly two " +
          "choices: Paris and Rome. After I answer, reply with one short sentence naming the city I chose."
      );
      await member.getByLabel("Send").click();
      const rome = member.getByRole("button", { name: /^Rome/ });
      await expect(rome.first()).toBeVisible({ timeout: TURN_TIMEOUT });
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
      await restore();
    }
  });
});
