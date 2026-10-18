import { defineConfig, devices } from "@playwright/test";

process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /web-tools\.(real|privacy-canary)\.spec\.ts/,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: process.env.E2E_WEB_OUTPUT_DIR || "test-results/web-tools",
  // The suites assert the Russian UI; pin it instead of the browser default.
  use: { locale: "en-US", trace: "off", screenshot: "off", video: "off" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
