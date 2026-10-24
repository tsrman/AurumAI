import { expect, type Page } from "@playwright/test";

export async function expectSelectedModel(page: Page, model: string): Promise<void> {
  await expect(page.getByRole("button", { name: "Choose model" })).toHaveText(model);
  await expect(page.getByLabel("Message")).toBeEnabled();
}

export async function expectNewAssistantReply(
  page: Page,
  previousReplyCount: number,
  timeoutMs: number
): Promise<void> {
  const replies = page.getByTestId("assistant-content");
  await expect(replies).toHaveCount(previousReplyCount + 1, { timeout: timeoutMs });
  await expect(replies.last()).toContainText(/\S/u, { timeout: timeoutMs });
}
