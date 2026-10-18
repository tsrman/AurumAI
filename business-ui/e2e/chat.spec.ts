import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

/**
 * Builds a scripted SSE body matching the real backend contract
 * (business-backend/src/routers/chat.py + conversation-runner event_publisher):
 * message-start -> content-delta* -> message-end -> done.
 * Only the LLM Gateway call is non-deterministic in this environment, so it's
 * the one thing stubbed here — conversation creation, auth and everything
 * else in this test hits the real backend.
 */
function sseBody(replyText: string): string {
  const words = replyText.split(" ");
  const deltas = words
    .map((word, i) => `event: content-delta\ndata: {"delta": "${i > 0 ? " " : ""}${word}"}\n\n`)
    .join("");
  return (
    `event: message-start\ndata: {}\n\n` +
    deltas +
    `event: message-end\ndata: {"content": "${replyText}"}\n\n` +
    `event: done\ndata: {}\n\n`
  );
}

test.describe("Chat", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("sending a message creates a conversation and streams the assistant answer", async ({ page }) => {
    const replyText = "Hello! How can I help?";

    await page.route("**/v1/chat/stream", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseBody(replyText),
      });
    });

    const composer = page.getByLabel("Message");
    await composer.fill("Hello");
    await page.getByLabel("Send").click();

    await expect(page.getByText("Hello", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(replyText)).toBeVisible({ timeout: 15_000 });
  });

  test("a generation failure (gateway unavailable) shows a banner and does not crash the page", async ({
    page,
  }) => {
    const pageErrors: Error[] = [];
    page.on("pageerror", (err) => pageErrors.push(err));

    await page.route("**/v1/chat/stream", async (route) => {
      await route.fulfill({ status: 500, body: "gateway unavailable" });
    });

    const composer = page.getByLabel("Message");
    await composer.fill("Break the generation");
    await page.getByLabel("Send").click();

    await expect(page.getByText("Break the generation", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(/gateway unavailable/i)).toBeVisible({ timeout: 15_000 });
    await composer.fill("One more message");
    await expect(composer).toHaveValue("One more message");
    expect(pageErrors).toEqual([]);
  });

  test("“Stop generating” interrupts the stream and signals /cancel on the backend", async ({
    page,
  }) => {
    let cancelCalled = false;

    // Never respond: simulate a stalled generation so the composer stays
    // in isStreaming until the user explicitly stops it.
    await page.route("**/v1/chat/stream", async () => {});
    await page.route("**/v1/chat/*/cancel", async (route) => {
      cancelCalled = true;
      await route.fulfill({ status: 204, body: "" });
    });

    await page.getByLabel("Message").fill("Long answer");
    await page.getByLabel("Send").click();

    const stopButton = page.getByLabel("Stop generating");
    await expect(stopButton).toBeVisible();
    await stopButton.click();

    await expect.poll(() => cancelCalled).toBe(true);
  });

  test("“Regenerate” requests a new answer and replaces the previous one", async ({
    page,
  }) => {
    let callCount = 0;
    await page.route("**/v1/chat/stream", async (route) => {
      callCount += 1;
      const reply =
        callCount === 1 ? "First answer" : "Second answer after regeneration";
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseBody(reply),
      });
    });

    await page.getByLabel("Message").fill("Hello");
    await page.getByLabel("Send").click();
    await expect(page.getByText("First answer")).toBeVisible({ timeout: 15_000 });

    await page.getByLabel("Regenerate").click();
    await expect(page.getByText("Second answer after regeneration")).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText("First answer")).not.toBeVisible();
    expect(callCount).toBe(2);
  });
});
