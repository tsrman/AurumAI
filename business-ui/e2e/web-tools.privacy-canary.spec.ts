import { writeFileSync } from "node:fs";
import { test } from "./web-tools-private-test";
import { expect } from "@playwright/test";

test.skip(!process.env.WEB_PRIVACY_CANARY || !process.env.WEB_PRIVACY_REACHED, "explicit isolated privacy canary only");

test("intentional private web acceptance failure", async ({ page, webPhase }) => {
  webPhase(process.env.WEB_PRIVACY_PHASE);
  await page.setContent("<button></button>");
  await page.getByRole("button").evaluate((element, content) => { element.textContent = content; }, process.env.WEB_PRIVACY_CANARY!);
  writeFileSync(process.env.WEB_PRIVACY_REACHED!, "page reached");
  // This ordinary locator assertion would leak its received page text if the
  // shared private-test wrapper or supported Playwright snapshot gate regressed.
  await expect(page.getByRole("button")).toHaveText("expected-safe-label", { timeout: 500 });
});
