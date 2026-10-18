import { defineConfig, devices } from "@playwright/test";

/**
 * Real end-to-end tests against the actual stack (Keycloak, backend, frontend).
 * Requires `docker compose up -d` (same prerequisite as ../smoke-test.sh) plus
 * the frontend reachable at PLAYWRIGHT_BASE_URL (defaults to the dev server).
 * The non-deterministic LLM Gateway call is stubbed at the browser network
 * boundary in chat.spec.ts; everything else (auth, conversations, settings)
 * hits the real backend.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    // The suites assert the Russian UI; pin it instead of the browser default.
    locale: "en-US",
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
