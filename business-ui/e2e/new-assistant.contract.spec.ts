import { expect, test } from "@playwright/test";

import { nextAssistantContent } from "./new-assistant";

test.use({ trace: "off", video: "off", screenshot: "off" });

test("selects the new assistant by stable identity after timestamp-skewed reorder", async ({ page }) => {
  await page.setContent(`
    <main>
      <article data-testid="assistant-message" aria-labelledby="assistant-title-old"
        data-created-at="2099-01-01T00:00:00Z">
        <div id="assistant-title-old">Assistant</div>
        <div data-testid="assistant-content">old answer</div>
      </article>
    </main>
  `);
  const next = await nextAssistantContent(page);

  await page.locator("main").evaluate((main) => {
    main.insertAdjacentHTML("afterbegin", `
      <article data-testid="assistant-message" aria-labelledby="assistant-title-new"
        data-created-at="2000-01-01T00:00:00Z">
        <div id="assistant-title-new">Assistant</div>
        <div data-testid="assistant-content">new answer</div>
      </article>
    `);
  });

  await expect(page.getByTestId("assistant-content").last()).toHaveText("old answer");
  await expect(next).toHaveCount(1);
  await expect(next).toHaveText("new answer");
});

test("excludes earlier durable messages when cancellation hydrates local identities", async ({ page }) => {
  await page.setContent(`<main>
    <article data-testid="assistant-message" aria-labelledby="assistant-title-local-old">
      <div data-testid="assistant-content">old answer</div>
    </article>
  </main>`);
  const next = await nextAssistantContent(page, ["durable-old"]);
  await page.locator("main").evaluate((main) => {
    main.innerHTML = `
      <article data-testid="assistant-message" aria-labelledby="assistant-title-durable-old">
        <div data-testid="assistant-content">old answer</div>
      </article>
      <article data-testid="assistant-message" aria-labelledby="assistant-title-new">
        <div data-testid="assistant-content">new answer</div>
      </article>`;
  });
  await expect(next).toHaveCount(1, { timeout: 1_000 });
  await expect(next).toHaveText("new answer");
});
