import { defineConfig, devices } from "@playwright/test";

/**
 * Real end-to-end tests against the actual stack (Keycloak, backend-admin,
 * admin-ui). Requires `docker compose up -d` plus admin-ui reachable at
 * PLAYWRIGHT_BASE_URL (defaults to the dev server on :3100).
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
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3100",
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
