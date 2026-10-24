import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";
import { expectNewAssistantReply, expectSelectedModel } from "./helpers/chat";

/**
 * Runs the chat flow against a real LLM gateway (e.g. a free OpenRouter model)
 * instead of stubbing /v1/chat/stream. Opt-in only: requires the backend to be
 * started with LLM_GATEWAY_URL/GATEWAY_API_KEY pointed at a real gateway, and
 * E2E_USE_REAL_LLM=true so the default `npm run test:e2e` run (no gateway
 * available) doesn't fail here. See README.md for setup.
 */
const ENABLED = process.env.E2E_USE_REAL_LLM === "true";
const MODEL = process.env.E2E_LLM_MODEL || "meta-llama/llama-3.1-8b-instruct:free";

test.describe("Chat with a real LLM", () => {
  test.skip(!ENABLED, "E2E_USE_REAL_LLM is not set; test skipped");

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test(`real answer from model ${MODEL}`, async ({ page }) => {
    // Default Playwright per-test timeout (30s) is shorter than the 60s this
    // test itself waits for the real LLM round trip below — without raising
    // it, the test always times out before its own assertion window closes,
    // regardless of how fast the model actually responds.
    test.setTimeout(90_000);

    await page.getByRole("button", { name: "Choose model" }).click();
    await page.getByRole("option", { name: MODEL, exact: true }).click();
    await expectSelectedModel(page, MODEL);

    const composer = page.getByLabel("Message");
    await composer.fill("Answer with one word: OK");
    const previousReplyCount = await page.getByTestId("assistant-content").count();
    await page.getByLabel("Send").click();

    await expect(
      page.getByText("Answer with one word: OK", { exact: true }).first()
    ).toBeVisible();

    await expectNewAssistantReply(page, previousReplyCount, 60_000);

    // No fixed reply text to assert on with a real model — wait for the
    // composer to leave streaming state (Stop button reverts to Send) as the
    // completion signal, then confirm no error banner surfaced.
    await expect(page.getByLabel("Send")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByLabel("Stop generating")).toHaveCount(0);
    await expect(page.getByText(/gateway unavailable/i)).toHaveCount(0);
    await page.reload();
    await expectSelectedModel(page, MODEL);
  });
});
