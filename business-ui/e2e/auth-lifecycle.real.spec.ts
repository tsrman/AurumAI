import { expect, test as base, type Page, type Request } from "@playwright/test";

// Manual acceptance against the existing stack. No realm/client/user mutations,
// API response stubs, saved credentials, traces, videos, or storageState files.
const ENABLED = process.env.E2E_AUTH_LIFECYCLE === "true";
const planes = [
  {
    name: "business",
    url: process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000",
    username: process.env.E2E_USERNAME || "user",
    password: process.env.E2E_PASSWORD || "user",
    landing: "/chat",
    link: "History",
    apiPath: "/v1/conversations",
  },
  {
    name: "admin",
    url: process.env.E2E_ADMIN_UI_URL || "http://localhost:3100",
    username: process.env.E2E_ADMIN_USERNAME || "admin",
    password: process.env.E2E_ADMIN_PASSWORD || "admin",
    landing: "/admin/users",
    link: "Agents",
    apiPath: "/v1/agent-definitions",
  },
] as const;
type Plane = (typeof planes)[number];
type AuthSessions = (plane: Plane) => Promise<Page>;

const test = base.extend<{ authSessions: AuthSessions }>({
  // A fixture owns cleanup independently of the test's timeout/cancellation.
  authSessions: [async ({ browser }, provide) => {
    const owned: { page: Page; plane: Plane }[] = [];
    try {
      await provide(async (plane) => {
        const context = await browser.newContext();
        const page = await context.newPage();
        owned.push({ page, plane });
        return page;
      });
    } finally {
      const outcomes = await Promise.allSettled(owned.map(async ({ page, plane }) => {
        try { await cleanupSession(page, plane); }
        finally { await page.context().close(); }
      }));
      const failures = outcomes.filter((outcome) => outcome.status === "rejected");
      if (failures.length) throw new AggregateError(failures.map((failure) => failure.reason), "Owned auth session cleanup failed");
    }
  }, { timeout: 60_000 }],
});

test.use({ trace: "off", screenshot: "off", video: "off" });
test.describe.configure({ timeout: 90_000 });
test.skip(!ENABLED, "Run smoke-test-auth-lifecycle.sh against the live stack");

async function signIn(page: Page, plane: Plane) {
  await page.goto(`${plane.url}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  // Requiring the credential form is intentional: logout must end SSO, not
  // merely clear React state while silently logging the same session back in.
  await page.locator("#username").fill(plane.username);
  await page.locator("#password").fill(plane.password);
  await page.locator("#kc-login").click();
  await expect.poll(() => new URL(page.url()).pathname).toBe(plane.landing);
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => Boolean(window.keycloak?.token))).toBe(true);
}

async function signedOut(page: Page) {
  await expect.poll(() => new URL(page.url()).pathname, { timeout: 20_000 }).toBe("/login");
  await expect(page.getByRole("button", { name: /sign in with keycloak/i })).toBeVisible();
}

async function logout(page: Page) {
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signedOut(page);
}

async function rememberToken(page: Page) {
  // Keep token material inside the browser. Assertion failures only print
  // booleans / timestamps, never bearer tokens or token endpoint bodies.
  return page.evaluate(() => {
    const token = window.keycloak?.token;
    if (!token) throw new Error("Authenticated browser has no access token");
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))) as {
      exp: number; sub: string; sid: string;
    };
    Object.assign(window, { __authLifecycleBefore: { token, ...payload } });
    return payload.exp * 1000;
  });
}

async function tokenChanged(page: Page) {
  return page.evaluate(() => {
    const before = (window as unknown as { __authLifecycleBefore: { token: string; sub: string; sid: string } }).__authLifecycleBefore;
    const token = window.keycloak?.token;
    if (!token || token === before.token) return false;
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))) as { sub: string; sid: string };
    return payload.sub === before.sub && payload.sid === before.sid;
  });
}

async function protectedNavigation(page: Page, plane: Plane) {
  const apiOrigin = await page.evaluate(() => new URL(window.__RUNTIME_CONFIG__!.apiUrl).origin);
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.origin === apiOrigin && url.pathname.replace(/\/$/, "") === plane.apiPath
      && response.request().method() === "GET"
      && ![301, 302, 303, 307, 308].includes(response.status());
  });
  // Client-side navigation preserves AuthProvider: reload would hide a stale
  // token propagation bug by doing a new SSO code exchange.
  await page.getByRole("link", { name: plane.link, exact: true }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const header = await response.request().headerValue("authorization");
  const usedCurrentToken = await page.evaluate(
    (authorization) => authorization === `Bearer ${window.keycloak?.token}`,
    header,
  );
  expect(usedCurrentToken).toBe(true);
}

function isRefresh(request: Request) {
  return new URL(request.url()).pathname.endsWith("/protocol/openid-connect/token")
    && request.method() === "POST"
    && new URLSearchParams(request.postData() || "").get("grant_type") === "refresh_token";
}

for (const plane of planes) {
  test.afterEach(async ({ page }, info) => {
    if (!info.title.startsWith(`${plane.name}:`)) return;
    await page.unrouteAll({ behavior: "wait" });
    await cleanupSession(page, plane);
  });
  test(`${plane.name}: logout blocks protected navigation and requires a fresh login`, async ({ page }) => {
      await signIn(page, plane);
      await logout(page);
      await page.goto(`${plane.url}${plane.landing}`);
      await signedOut(page);
      await signIn(page, plane);
      await protectedNavigation(page, plane);
  });

  test(`${plane.name}: forced refresh propagates the new token to actual UI requests`, async ({ page }) => {
      await signIn(page, plane);
      await rememberToken(page);
      const refreshed = await page.evaluate(() => window.keycloak!.updateToken(-1));
      expect(refreshed).toBe(true);
      await expect.poll(() => tokenChanged(page)).toBe(true);
      await protectedNavigation(page, plane);
  });

  test(`${plane.name}: temporary refresh network failure preserves login and recovers`, async ({ page }) => {
    await page.clock.install();
      await signIn(page, plane);
      const expiresAt = await rememberToken(page);
      await page.route("**/protocol/openid-connect/token", async (route) => {
        if (isRefresh(route.request())) await route.abort("internetdisconnected");
        else await route.continue();
      });
      const failed = page.waitForEvent("requestfailed", isRefresh);
      // Fault-injection scenario, not evidence of natural expiry. Advancing
      // only this browser makes the existing background refresh timer due.
      await page.clock.fastForward(Math.max(60_000, expiresAt - Date.now() + 1000));
      await failed;
      await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
      expect(new URL(page.url()).pathname).toBe(plane.landing);
      await page.unrouteAll({ behavior: "wait" });
      // Advance in increments until the actual background retry succeeds.
      // A retained token does NOT prove that catch scheduled its backoff yet.
      await expect.poll(async () => {
        await page.clock.runFor(10_000);
        return tokenChanged(page);
      }, { timeout: 20_000, intervals: [100] }).toBe(true);
      await protectedNavigation(page, plane);
  });

  test(`${plane.name}: logout in another tab invalidates only this browser SSO session`, async ({ page, context }) => {
    await page.clock.install();
      await signIn(page, plane);
      const other = await context.newPage();
      await other.goto(`${plane.url}${plane.landing}`);
      await expect.poll(() => other.evaluate(() => Boolean(window.keycloak?.token))).toBe(true);
      await expect(other.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
      await logout(other);
      await other.close();
      // Real RP-initiated logout above; no realm admin logout-all call.
      await page.clock.fastForward(60_000);
      await signedOut(page);
      await signIn(page, plane);
      await protectedNavigation(page, plane);
  });
}

test("both planes: natural access-token expiry keeps the session and UI API access", async ({ authSessions }) => {
  test.setTimeout(900_000);
  // Independent cookie jars; waiting overlaps but the test is a single worker.
  // No fake clock, updateToken call, reload, or token endpoint interception.
  const outcomes = await Promise.allSettled(planes.map(async (plane) => {
      const page = await authSessions(plane);
      await signIn(page, plane);
      const expiresAt = await rememberToken(page);
      const remaining = expiresAt - Date.now();
      expect(remaining).toBeGreaterThan(0);
      expect(remaining).toBeLessThan(780_000);
      await expect.poll(() => tokenChanged(page), { timeout: remaining + 75_000, intervals: [1000] }).toBe(true);
      // Prove use after the ORIGINAL token has actually expired, not just a
      // proactive refresh before expiry. Polling uses real host time.
      await expect.poll(() => Date.now() > expiresAt + 1000, {
        timeout: Math.max(5000, expiresAt - Date.now() + 5000), intervals: [1000],
      }).toBe(true);
      const expiredStatus = await page.evaluate(async () => {
        const before = (window as unknown as { __authLifecycleBefore: { token: string } }).__authLifecycleBefore;
        const response = await fetch(`${window.__RUNTIME_CONFIG__!.apiUrl.replace(/\/$/, "")}/auth/me`, {
          headers: { Authorization: `Bearer ${before.token}` }, credentials: "omit",
        });
        return response.status;
      });
      expect(expiredStatus).toBe(401);
      await protectedNavigation(page, plane);
  }));
  const failures = outcomes.filter((outcome) => outcome.status === "rejected");
  if (failures.length) throw new AggregateError(failures.map((failure) => failure.reason), "Natural expiry acceptance failed");
});

async function cleanupSession(page: Page, plane: Plane) {
  // End only the session owned by this browser context, including on failure.
  // Do not clear cookies first: that would leave a live server-side session.
  await page.goto(`${plane.url}${plane.landing}`);
  await expect.poll(async () => {
    if (new URL(page.url()).pathname === "/login") return "signed-out";
    if (!await page.evaluate(() => Boolean(window.keycloak?.token))) return "loading";
    return await page.getByRole("button", { name: "Sign out", exact: true }).isVisible() ? "ready" : "loading";
  }).not.toBe("loading");
  if (new URL(page.url()).pathname !== "/login") await logout(page);
}
