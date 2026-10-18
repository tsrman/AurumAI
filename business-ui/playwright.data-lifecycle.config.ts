import { defineConfig, devices } from "@playwright/test";

process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1";
export default defineConfig({
  testDir: "./e2e",
  testMatch: /data-lifecycle\.real\.spec\.ts/,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "test-results/data-lifecycle",
  // The suites assert the Russian UI; pin it instead of the browser default.
  use: { locale: "en-US", trace: "off", screenshot: "off", video: "off", storageState: undefined },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
