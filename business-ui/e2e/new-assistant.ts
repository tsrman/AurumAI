import type { Locator, Page } from "@playwright/test";

export async function nextAssistantContent(page: Page, priorDurableIds: string[] = []): Promise<Locator> {
  const identities = await page.getByTestId("assistant-message").evaluateAll((articles) =>
    articles.map((article) => article.getAttribute("aria-labelledby"))
  );
  if (identities.some((identity) =>
    typeof identity !== "string" || !/^assistant-title-[A-Za-z0-9_-]+$/.test(identity)
  )) {
    throw new Error("assistant identity unavailable");
  }
  const captured = identities as string[];
  if (new Set(captured).size !== captured.length) {
    throw new Error("assistant identity is not unique");
  }
  if (priorDurableIds.some(id => typeof id !== "string" || !/^[A-Za-z0-9_-]+$/.test(id))) {
    throw new Error("durable assistant identity unavailable");
  }
  // History hydration can replace old local aN identities with persisted UUIDs
  // after capture. Neither representation belongs to the next response.
  const exclusions = [...new Set([...captured, ...priorDurableIds.map(id => `assistant-title-${id}`)])]
    .map((identity) => `:not([aria-labelledby="${identity}"])`)
    .join("");
  return page
    .locator(`[data-testid="assistant-message"]${exclusions}`)
    .getByTestId("assistant-content");
}
