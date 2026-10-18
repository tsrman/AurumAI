import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

/**
 * Mirrors the SSE stub in chat.spec.ts — only the LLM Gateway call is
 * stubbed; conversation creation, /history listing, rename, delete and
 * navigation all hit the real backend.
 */
function sseBody(replyText: string): string {
  return (
    `event: message-start\ndata: {}\n\n` +
    `event: content-delta\ndata: {"delta": "${replyText}"}\n\n` +
    `event: message-end\ndata: {"content": "${replyText}"}\n\n` +
    `event: done\ndata: {}\n\n`
  );
}

test.describe("Conversation history", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("a new conversation appears in history, opens, is renamed and deleted", async ({
    page,
  }) => {
    const title = `E2E history ${Date.now()}`;

    await page.route("**/v1/chat/stream", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseBody("Assistant answer"),
      });
    });

    await page.getByLabel("Message").fill(title);
    await page.getByLabel("Send").click();
    await expect(page.getByText("Assistant answer")).toBeVisible({
      timeout: 15_000,
    });

    // Scope to main: Sidebar history also renders conversation-open buttons,
    // so a page-wide locator would be ambiguous.
    await page.goto("/history");
    const main = page.locator("main");
    const openButton = main.getByRole("button", {
      name: `Open conversation ${title}`,
    });
    await expect(openButton).toBeVisible({ timeout: 15_000 });

    // Opening a conversation from /history must not 404: the chat page
    // reads the active conversation from context, not from the URL.
    await openButton.click();
    await page.waitForURL(/\/chat$/);
    await expect(page.getByLabel("Message")).toBeVisible();

    await page.goto("/history");

    // Rename through the actions menu, which is portaled to
    // <body> (not a DOM descendant of `item`), so it's queried page-wide.
    const item = main
      .getByRole("button", { name: `Open conversation ${title}` })
      .locator("xpath=./..");
    await item.getByLabel("Actions").click();
    await page.getByRole("menuitem", { name: "Rename" }).click();

    const renamedTitle = `${title} (renamed)`;
    // Not scoped through `item`: entering edit mode clears the open-button's
    // aria-label (see HistoryItem.tsx), which `item` is derived from, so
    // re-resolving through it here would time out. Only one item is ever in
    // edit mode at a time, so this stays unambiguous.
    const input = main.getByLabel("Conversation title");
    await input.fill(renamedTitle);
    await input.press("Enter");

    await expect(
      main.getByRole("button", { name: `Open conversation ${renamedTitle}` })
    ).toBeVisible();

    await page.reload();
    await expect(
      main.getByRole("button", { name: `Open conversation ${renamedTitle}` })
    ).toBeVisible({ timeout: 15_000 });

    // Delete via the menu — confirmed through the app's own ConfirmDialog
    // (not the native window.confirm()).
    const renamedItem = main
      .getByRole("button", { name: `Open conversation ${renamedTitle}` })
      .locator("xpath=./..");
    await renamedItem.getByLabel("Actions").click();
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page
      .getByRole("alertdialog", { name: /Delete the conversation\?/i })
      .getByRole("button", { name: "Delete" })
      .click();

    await expect(
      main.getByRole("button", { name: `Open conversation ${renamedTitle}` })
    ).not.toBeVisible({ timeout: 15_000 });

    await page.reload();
    await expect(
      main.getByRole("button", { name: `Open conversation ${renamedTitle}` })
    ).not.toBeVisible();
  });
});
