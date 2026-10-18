import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, type Page } from "@playwright/test";
import { test } from "./web-tools-private-test";
import { initialTurnFailurePhase, verifyInitialTurnStream, verifyReconnect, verifyLiveRevocation } from "./web-tools-assertions";
import { verifyWebFaults } from "./web-fault-scenarios";
import { verifyInteractiveMultiAgent } from "./interactive-multi-agent-scenarios";
import { nextAssistantContent } from "./new-assistant";

const ENABLED = process.env.E2E_WEB_TOOLS === "true";
const STATE = process.env.E2E_WEB_TOOLS_STATE || "";
const MARKER = process.env.E2E_WEB_TOOLS_MARKER || "";
const KIND = process.env.WEB_ACCEPTANCE_KIND;
const BUSINESS = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const API = process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
const ROOT = path.resolve(__dirname, "../..");
type Source = { sourceId: string; kind: string; finalUrl: string; retrievedAt: string };
type Message = { id: string; role: string; content: string; turnId?: string; sourceIds?: string[]; unsupportedSourceIds?: string[] };
type History = { messages: { data: Message[] } };

function fixture(command: string, id?: string, action?: string) {
  try {
    return execFileSync(process.env.E2E_WEB_TOOLS_PYTHON || "python3", [process.env.E2E_WEB_TOOLS_FIXTURE_SCRIPT || path.join(ROOT, "scripts/web_tools_e2e.py"), command,
      "--state", STATE, "--marker", MARKER, ...(id ? ["--id", id] : []), ...(action ? ["--action", action] : [])],
    { cwd: ROOT, env: process.env, stdio: ["ignore", "pipe", "pipe"], encoding: "utf8" });
  } catch {
    throw new Error("owned fixture operation failed; raw diagnostics suppressed");
  }
}

async function login(page: Page, admin = false) {
  // Even the ownership-negative administrator uses a business-ui audience.
  await page.goto(`${BUSINESS}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.locator("#username").fill(admin ? process.env.E2E_ADMIN_USERNAME || "admin" : process.env.E2E_MEMBER_USERNAME || "user");
  await page.locator("#password").fill(admin ? process.env.E2E_ADMIN_PASSWORD || "admin" : process.env.E2E_MEMBER_PASSWORD || "user");
  await page.locator("#kc-login").click();
  await page.waitForURL(`${BUSINESS}/chat`);
  await expect.poll(() => page.evaluate(() => Boolean(window.keycloak?.token))).toBe(true);
}

async function api<T>(page: Page, endpoint: string, method = "GET", body?: unknown, expected = 200): Promise<T> {
  const result = await page.evaluate(async ({ url, method, body }) => {
    const response = await fetch(url, { method,
      headers: { Authorization: `Bearer ${window.keycloak?.token}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, payload: await response.json().catch(() => null) };
  }, { url: `${API}${endpoint}`, method, body });
  // Never place a response body/JWT/query into assertion diagnostics.
  if (result.status !== expected) {
    const status = [400, 401, 403, 404, 409, 410, 422, 429, 500, 502, 503, 504]
      .find(code => code === result.status) ?? "other";
    process.stdout.write(`[web-api-status:${status}]\n`);
  }
  expect(result.status, "web acceptance API status").toBe(expected);
  return result.payload?.data as T;
}

test.use({ trace: "off", video: "off", screenshot: "off" });
test.skip(!ENABLED, "requires exclusive web-tools wrapper and private recovery journal");

test("real web boundary, source ownership/history, limits and live revoke", async ({ browser, webPhase }) => {
  test.setTimeout(600_000);
  expect(MARKER).toMatch(/^workflow-templates-ui-e2e-[a-f0-9]{16}$/);
  expect(["controlled", "public"]).toContain(KIND);
  const state = JSON.parse(readFileSync(STATE, "utf8")) as { marker: string; prepared: boolean };
  expect(state.marker === MARKER && state.prepared, "owned prepared fixture").toBe(true);
  const memberContext = await browser.newContext();
  const ownerNegativeContext = await browser.newContext();
  try {
    const member = await memberContext.newPage();
    const admin = await ownerNegativeContext.newPage();
    webPhase("login");
    await login(member);
    await login(admin, true);

    async function create(suffix: "run-v1" | "run-v2") {
      const title = `${MARKER}-${suffix}`;
      const conversation = await api<{ id: string }>(member, "/v1/conversations", "POST", { title, model: `${MARKER}-model` });
      fixture("record", conversation.id); // durable identity BEFORE the first turn
      await member.goto(`${BUSINESS}/history`);
      await member.getByRole("main").getByText(title, { exact: true }).click();
      await member.getByRole("button", { name: "Choose model" }).click();
      await member.getByRole("option", { name: new RegExp(`^${MARKER}-model(?:\\s|$)`) }).click();
      return conversation.id;
    }

    async function turn(prompt: string, expected: string, conversation: string, phase?: string) {
      const before = await api<History>(member, `/v1/conversations/${conversation}`);
      const priorTurns = new Set(before.messages.data.map(message => message.turnId));
      const newAssistant = await nextAssistantContent(member,
        before.messages.data.filter(message => message.role === "assistant").map(message => message.id));
      if (phase === "sources-turn") webPhase("sources-turn-send");
      const pending = member.waitForResponse(response => response.url() === `${API}/v1/chat/stream`
        && response.request().method() === "POST");
      await member.getByLabel("Message").fill(prompt);
      await member.getByLabel("Send").click();
      const response = await pending;
      expect(response.status()).toBe(200);
      expect(response.request().postDataJSON().workflowId ?? null,
        "ordinary web scenario must not retain a selected workflow").toBeNull();
      if (phase === "sources-turn") webPhase("sources-turn-stream");
      if (phase === "mixed") webPhase("mixed-question-web-stream");
      if (phase?.startsWith("fault-")) webPhase(`${phase}-stream`);
      try {
        await verifyInitialTurnStream(response, expected);
      } catch (error) {
        if (phase === "sources-turn") webPhase(initialTurnFailurePhase(error));
        if (phase === "errors-pdf" && error instanceof Error) {
          if (error.message === "turn stream outcome web-unavailable") webPhase("errors-pdf-web-unavailable");
          else if (error.message === "turn stream outcome web-timeout") webPhase("errors-pdf-web-timeout");
          else if (error.message === "turn stream outcome final") webPhase("errors-pdf-final");
          else webPhase("errors-pdf-marker");
        }
        throw error;
      }
      if (phase === "sources-turn") webPhase("sources-turn-render");
      if (phase === "mixed") webPhase("mixed-question-web-render");
      if (phase?.startsWith("fault-")) webPhase(`${phase}-render-count`);
      await expect(newAssistant).toHaveCount(1, { timeout: 120_000 });
      if (phase?.startsWith("fault-")) webPhase(`${phase}-render-marker`);
      await expect(newAssistant).toContainText(expected, { timeout: 120_000 });
      if (phase === "sources-turn") webPhase("sources-turn-history");
      if (phase === "mixed") webPhase("mixed-question-web-history");
      if (phase?.startsWith("fault-")) webPhase(`${phase}-history`);
      const history = await api<History>(member, `/v1/conversations/${conversation}`);
      const newAssistants = history.messages.data.filter(message =>
        message.role === "assistant" && message.turnId && !priorTurns.has(message.turnId));
      expect(newAssistants.length, "exactly one new durable assistant").toBe(1);
      const assistant = newAssistants[0];
      expect(Boolean(assistant?.content.startsWith(expected)), "durable final marker").toBe(true);
      expect(Boolean(assistant?.turnId), "durable turn identity").toBe(true);
      if (process.env.E2E_WEB_TOOLS_RUNTIME === "kubernetes") fixture("pin", conversation);
      return { ...assistant!, turnId: assistant!.turnId! };
    }

    webPhase("sources-create");
    const conversation = await create("run-v1");
    webPhase("sources-turn");
    const first = await turn("WEB_E2E_SEARCH", "WEB_E2E_FINAL", conversation, "sources-turn");
    webPhase("sources-api");
    const endpoint = `/v1/conversations/${conversation}/turns/${first.turnId}/sources`;
    const sources = await api<Source[]>(member, endpoint);
    expect(sources.some(source => source.kind === "search"), "real search produced sources").toBe(true);
    expect(sources.some(source => source.kind === "read"), "real read produced a source").toBe(true);
    expect(sources.every(source => /^https?:\/\//.test(source.finalUrl) && !Number.isNaN(Date.parse(source.retrievedAt))), "safe provenance").toBe(true);
    expect((first.sourceIds?.length || 0) > 0, "server-accepted citations").toBe(true);
    webPhase("sources-owner");
    await api(admin, endpoint, "GET", undefined, 404); // org_admin is not conversation owner

    webPhase("sources-ui");
    const sourceButton = member.getByRole("button", { name: /^Source 1:/ }).first();
    await expect(sourceButton).toBeVisible({ timeout: 30_000 });
    await sourceButton.click({ timeout: 5_000 });
    const link = member.getByRole("dialog").getByRole("link", { name: "Open source" });
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    await expect(link).toHaveAttribute("referrerpolicy", "no-referrer");
    await member.getByRole("button", { name: "Close", exact: true }).click();
    webPhase("history-reload");
    await member.reload();
    webPhase("history-reload-auth");
    await expect.poll(() => member.evaluate(() => Boolean(window.keycloak?.token)),
      { timeout: 30_000, message: "authentication restored after reload" }).toBe(true);
    webPhase("history-navigation");
    await member.goto(`${BUSINESS}/history`);
    webPhase("history-navigation-auth");
    await expect.poll(() => member.evaluate(() => Boolean(window.keycloak?.token)),
      { timeout: 30_000, message: "history authentication ready" }).toBe(true);
    webPhase("history-open");
    await member.getByRole("main").getByRole("button", {
      name: `Open conversation ${MARKER}-run-v1`, exact: true,
    }).click({ timeout: 30_000 });
    webPhase("history-sources");
    await expect(member.getByRole("button", { name: /^Source 1:/ }).first()).toBeVisible();
    const reloaded = await api<Source[]>(member, endpoint);
    expect(reloaded.map(source => source.sourceId).sort()).toEqual(sources.map(source => source.sourceId).sort());

    if (KIND === "controlled") {
      webPhase("reconnect");
      const before = await api<History>(member, `/v1/conversations/${conversation}`);
      const beforeCount = before.messages.data.filter(message => message.role === "assistant").length;
      const beforeCitations = await member.getByRole("button", { name: /^Source \d+:/ }).count();
      const pending = member.waitForResponse(response => response.url() === `${API}/v1/chat/stream`
        && response.request().method() === "POST");
      await member.getByLabel("Message").fill("WEB_E2E_RECONNECT");
      await member.getByLabel("Send").click();
      expect((await pending).status()).toBe(200);
      await expect(member.getByText("Reading page", { exact: true }).last()).toBeVisible();
      // Drop the original POST stream while the real reader is waiting; the
      // reopened conversation must establish a fresh durable turn-event SSE.
      await member.reload();
      const reconnected = member.waitForResponse(response => /\/turns\/[^/]+\/events/.test(response.url()));
      await member.goto(`${BUSINESS}/history`);
      await member.getByRole("main").getByText(`${MARKER}-run-v1`, { exact: true }).click();
      const response = await reconnected;
      const turnId = /\/turns\/([^/]+)\/events/.exec(response.url())![1];
      await verifyReconnect(response, turnId, async () => {
        // No navigation or API history polling may repair a failed stream.
        // This view must consume terminal SSE and render the final + sources.
        await expect.poll(async () =>
          await member.getByTestId("assistant-content").count() === beforeCount + 1
          && (await member.getByTestId("assistant-content").last().innerText()).includes("WEB_E2E_FINAL")
          && await member.getByRole("button", { name: /^Source \d+:/ }).count() > beforeCitations,
        { timeout: 90_000, message: "reconnected active view consumed final/source state" }).toBe(true);
        return true;
      });
      if (process.env.E2E_WEB_TOOLS_RUNTIME === "kubernetes") fixture("pin", conversation);
      webPhase("errors");
      webPhase("errors-hostile");
      await turn("WEB_E2E_HOSTILE", "WEB_E2E_FINAL", conversation);
      webPhase("errors-pdf");
      await turn("WEB_E2E_PDF", "WEB_E2E_ERROR:unsupported_document", conversation, "errors-pdf");
      webPhase("errors-html");
      await turn("WEB_E2E_HTML", "WEB_E2E_ERROR:unsupported_document", conversation);
      webPhase("errors-binary");
      await turn("WEB_E2E_BINARY", "WEB_E2E_ERROR:unsupported_document", conversation);
      webPhase("errors-large");
      await turn("WEB_E2E_LARGE", "WEB_E2E_ERROR:web_too_large", conversation);
      webPhase("errors-redirect");
      await turn("WEB_E2E_REDIRECT", "WEB_E2E_ERROR:web_url_blocked", conversation);
      webPhase("mixed-question-web");
      const mixed = turn("WEB_E2E_MIXED", "WEB_E2E_FINAL ANSWER:IANA", conversation, "mixed");
      // Attach rejection handling immediately while the UI is waiting for input.
      const outcome = mixed.then(value => ({ value }), error => ({ error }));
      await expect(member.getByText("Which source topic?", { exact: true })).toBeVisible({ timeout: 60_000 });
      await member.getByRole("textbox", { name: "Answer", exact: true }).fill("IANA");
      await member.getByRole("button", { name: "Answer", exact: true }).click();
      const result = await outcome;
      if ("error" in result) throw result.error;
      const mixedSources = await api<Source[]>(member, `/v1/conversations/${conversation}/turns/${result.value.turnId}/sources`);
      expect(mixedSources.map(source => source.kind).sort()).toEqual(["read", "search"]);
      expect(result.value.sourceIds).toHaveLength(2);
      webPhase("upstream-faults");
      await verifyWebFaults({ page: member, conversation,
        api: <T>(endpoint: string) => api<T>(member, endpoint), fixture,
        turn: (prompt, expected, phase) => turn(prompt, expected, conversation, phase),
        phase: webPhase,
      });
    }
    await verifyInteractiveMultiAgent({
      page: member,
      conversation,
      workflowName: `${MARKER}-source`,
      controlled: KIND === "controlled",
      api: <T>(endpoint: string, method?: string, body?: unknown, expected?: number) =>
        api<T>(member, endpoint, method, body, expected),
      fixture,
      phase: webPhase,
    });
    // Explicitly establish the ordinary route for the checks below, independent
    // of the composer's current selection/reset behavior.
    await member.getByRole("button", { name: "Choose workflow" }).click();
    await member.getByRole("option", { name: /^Plain chat(?:\s|$)/ }).click();
    webPhase("limits");
    await turn("WEB_E2E_LIMIT", "WEB_E2E_ERROR:web_limit", conversation);
    // Observe the server-owned barrier after admission with web tools in the
    // current system prompt. No sleeps and no permission snapshot assumption.
    webPhase("revoke");
    await verifyLiveRevocation({
      arm: async () => fixture("barrier-arm"),
      send: () => turn("WEB_E2E_REVOKE", "WEB_E2E_ERROR:web_access_denied", conversation),
      reached: () => expect.poll(() => JSON.parse(fixture("barrier-status")).reached === true,
        { timeout: 60_000, message: "admitted web turn reached owned gateway barrier" }).toBe(true),
      revoke: async () => fixture("revoke"),
      release: async () => fixture("barrier-release"),
      hidden: () => turn("WEB_E2E_TOOLS_HIDDEN", "WEB_E2E_TOOLS_HIDDEN_OK", conversation),
      grant: async () => fixture("grant"),
    });
    webPhase("ownership");
    const other = await create("run-v2");
    const wrong = await turn(`WEB_E2E_WRONG:${sources[0].sourceId}`, "WEB_E2E_WRONG", other);
    expect(wrong.sourceIds || []).toHaveLength(0);
    expect(wrong.unsupportedSourceIds).toEqual([sources[0].sourceId]);
    await member.getByRole("button", { name: /unverified/ }).click();
    await expect(member.getByRole("dialog").getByRole("link")).toHaveCount(0);
    await api(member, `/v1/conversations/${other}/turns/${first.turnId}/sources`, "GET", undefined, 404);
    webPhase("cleanup");
  } finally {
    await Promise.allSettled([memberContext.close(), ownerNegativeContext.close()]);
    // Only the wrapper may restore grants and close exact Temporal trees.
  }
});
