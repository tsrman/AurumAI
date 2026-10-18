import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { test, expect, type Page } from "@playwright/test";
import { login } from "./helpers/auth";
import { expectNewAssistantReply } from "./helpers/chat";

/**
 * Real-provider long dialogue with26 turns, MCP and optional durable questions/web.
 * Requires E2E_USE_REAL_LLM=true, E2E_LLM_MODEL, a configured gateway and matching
 * stack credentials. The MCP segment needs the development mcp profile.
 * Questions/web have independent opt-in flags and server-owned admission switches;
 * deploy compatible API/runner/UI before enabling them. Web also needs the isolated
 * boundary, secrets, live web.access grants and operator policy enforcement.
 * This test snapshots/restores its owned resources/grants. Do not run alongside
 * organization-mutating/browser wrappers. Each tool turn verifies a fresh owned
 * result; final assertions require complete paginated REST history and reload.
 * Deterministic controlled acceptance does not imply this paid-provider combination.
 */
const ENABLED = process.env.E2E_USE_REAL_LLM === "true";
const WEB_ENABLED = process.env.E2E_LONG_DIALOG_WEB === "true";
const QUESTIONS_ENABLED = process.env.E2E_LONG_DIALOG_QUESTIONS === "true";
const MODEL = process.env.E2E_LLM_MODEL || "";
if (ENABLED && !MODEL) {
  throw new Error("Set E2E_LLM_MODEL when E2E_USE_REAL_LLM=true.");
}
const REQUESTED_TURNS = Number.parseInt(process.env.E2E_LONG_DIALOG_TURNS || "26", 10) || 26;
const POSTGRES_CONTAINER = process.env.E2E_POSTGRES_CONTAINER || "aurum-ai-postgres";
const ADMIN_BACKEND_URL = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const BUSINESS_API = process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
const KEYCLOAK_URL = process.env.E2E_KEYCLOAK_URL || "http://localhost:8080";
const KEYCLOAK_REALM = process.env.E2E_KEYCLOAK_REALM || "aurum-ai";

// One real round trip per turn realistically takes anywhere from a few
// seconds (plain Q&A) to well over a minute (a tool call plus the
// follow-up synthesis call). These are generous per-turn ceilings, not
// expected latencies.
const FILLER_TIMEOUT_MS = 45_000;
const SKILL_TIMEOUT_MS = 90_000;
const WEB_TIMEOUT_MS = 120_000;
const QUESTION_VISIBLE_TIMEOUT_MS = 90_000;
const QUESTION_ANSWER_TIMEOUT_MS = 90_000;

function orgIdFromToken(token: string): string {
  const payload = token.split(".")[1];
  const padded = payload + "=".repeat((4 - (payload.length % 4)) % 4);
  const json = Buffer.from(padded, "base64url").toString("utf-8");
  return JSON.parse(json).org_id as string;
}

function psql(sql: string): void {
  execFileSync("docker", [
    "exec",
    "-i",
    POSTGRES_CONTAINER,
    "psql",
    "-U",
    "postgres",
    "-d",
    "aurum_ai",
    "-v",
    "ON_ERROR_STOP=1",
    "-c",
    sql,
  ]);
}

async function getAdminUiToken(): Promise<string> {
  const res = await fetch(
    `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token`,
    {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "password",
        client_id: "admin-ui",
        username: process.env.E2E_ADMIN_USERNAME || "admin",
        password: process.env.E2E_ADMIN_PASSWORD || "admin",
      }),
    }
  );
  const json = (await res.json()) as { access_token?: string };
  if (!json.access_token) throw new Error("Could not get an admin-ui token from Keycloak");
  return json.access_token;
}

async function getOrgCapabilities(adminToken: string): Promise<string[]> {
  const res = await fetch(`${ADMIN_BACKEND_URL}/v1/admin/authorization/capabilities/organization`, {
    headers: { Authorization: `Bearer ${adminToken}` },
  });
  if (!res.ok) throw new Error(`GET org capabilities failed: ${res.status}`);
  const json = (await res.json()) as { data: { capabilities: string[] } };
  return json.data.capabilities;
}

async function putOrgCapabilities(adminToken: string, capabilities: string[]): Promise<void> {
  const res = await fetch(`${ADMIN_BACKEND_URL}/v1/admin/authorization/capabilities/organization`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${adminToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ capabilities }),
  });
  if (!res.ok) throw new Error(`PUT org capabilities failed: ${res.status}`);
}

/** Grants `web.access` at the organization level (additive with any
 *  existing capabilities) and returns a restorer that removes exactly what
 *  this call added — never a blind "set back to empty". */
async function grantWebAccess(): Promise<() => Promise<void>> {
  const adminToken = await getAdminUiToken();
  const before = await getOrgCapabilities(adminToken);
  if (before.includes("web.access")) {
    return async () => {}; // already granted by the operator — not ours to revoke
  }
  await putOrgCapabilities(adminToken, [...before, "web.access"]);
  return async () => {
    const freshToken = await getAdminUiToken();
    const current = await getOrgCapabilities(freshToken);
    await putOrgCapabilities(
      freshToken,
      current.filter((capability) => capability !== "web.access")
    );
  };
}

async function apiAs<T>(
  page: Page,
  endpoint: string,
  method = "GET",
  body?: unknown,
  expected = 200
): Promise<T> {
  const result = await page.evaluate(
    async ({ url, method, body }) => {
      const response = await fetch(url, {
        method,
        headers: {
          Authorization: `Bearer ${window.keycloak?.token}`,
          "Content-Type": "application/json",
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      return { status: response.status, payload: await response.json().catch(() => null) };
    },
    { url: `${BUSINESS_API}${endpoint}`, method, body }
  );
  expect(result.status, `${method} ${endpoint}`).toBe(expected);
  return result.payload?.data as T;
}

type Source = { sourceId: string; kind: string; finalUrl: string };
type Question = {
  questionId: string;
  turnId: string;
  text: string;
  status: "pending" | "answered" | "expired" | "cancelled";
};
type HistoryMessage = { id: string; role: string; turnId?: string };
type History = { messages: {
  data: HistoryMessage[];
  pagination: { hasMore: boolean; cursor: string | null };
} };

async function getHistory(page: Page, conversationId: string): Promise<History> {
  const messages: HistoryMessage[] = [];
  const cursors = new Set<string>();
  let cursor: string | null = null;
  while (true) {
    const suffix: string = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    const history = await apiAs<History>(page, `/v1/conversations/${conversationId}${suffix}`);
    messages.push(...history.messages.data);
    const pagination = history.messages.pagination;
    if (!pagination.hasMore) return { messages: { data: messages, pagination } };
    expect(pagination.cursor, "next history page has a cursor").toBeTruthy();
    expect(cursors.has(pagination.cursor!), "history pagination advances").toBe(false);
    cursor = pagination.cursor!;
    cursors.add(cursor);
  }
}

/** Sends one message and waits for exactly one new assistant reply,
 *  asserting the platform-level completion signals every turn must satisfy
 *  regardless of what the model actually said. Returns the reply's text and
 *  the running assistant-reply count for the next call. */
async function sendTurn(
  page: Page,
  prompt: string,
  previousReplyCount: number,
  timeoutMs: number
): Promise<{ text: string; replyCount: number }> {
  await page.getByLabel("Message").fill(prompt);
  await page.getByLabel("Send").click();
  await expectNewAssistantReply(page, previousReplyCount, timeoutMs);
  const replyCount = previousReplyCount + 1;
  await expect(page.getByLabel("Send")).toBeVisible({ timeout: timeoutMs });
  await expect(page.getByLabel("Stop generating")).toHaveCount(0);
  await expect(page.getByText(/gateway unavailable/i)).toHaveCount(0);
  const text = (await page.getByTestId("assistant-content").nth(replyCount - 1).innerText()) ?? "";
  return { text, replyCount };
}

function randomOperand(): number {
  return 10 + Math.floor(Math.random() * 89); // two digits — trivial for any real model
}

test.describe("Long conversation with a real model: skills, web tools, questions", () => {
  test.skip(!ENABLED, "E2E_USE_REAL_LLM is not set; test skipped");

  let skillName = "";
  let skillId = "";
  let restoreWebAccess: (() => Promise<void>) | null = null;

  test.beforeAll(async () => {
    skillName = `e2e-long-dialog-sum-${Date.now()}`;
    skillId = randomUUID();
    if (WEB_ENABLED) {
      restoreWebAccess = await grantWebAccess();
    }
  });

  test.afterAll(async () => {
    // Best-effort: the skill row may never have been created if login
    // failed before seeding, and the org may already have had web.access.
    try {
      psql(
        `DELETE FROM access_grants WHERE resource_type = 'skill' AND resource_id = '${skillId}'; ` +
          `DELETE FROM skills WHERE id = '${skillId}';`
      );
    } catch {
      // Nothing to clean up if the skill was never seeded.
    }
    if (restoreWebAccess) await restoreWebAccess();
  });

  test(`~${REQUESTED_TURNS} exchanges through model ${MODEL}`, async ({ page }) => {
    const specialSegments = 1 + (WEB_ENABLED ? 2 : 0) + (QUESTIONS_ENABLED ? 1 : 0);
    const totalTurns = Math.max(REQUESTED_TURNS, specialSegments + 6);
    // ~25s/filler turn + generous ceilings for each tool-using turn, plus
    // slack for login/setup — matches the budget web-tools.real.spec.ts
    // uses for its own multi-phase real-infra test.
    test.setTimeout(
      60_000 +
        totalTurns * FILLER_TIMEOUT_MS +
        (WEB_ENABLED ? 2 * WEB_TIMEOUT_MS : 0) +
        (QUESTIONS_ENABLED ? QUESTION_ANSWER_TIMEOUT_MS : 0)
    );

    await login(page);
    const token = await page.evaluate(() => window.keycloak?.token);
    if (!token) throw new Error("window.keycloak.token is unavailable after login");
    const orgId = orgIdFromToken(token);

    psql(
      `INSERT INTO skills (id, org_id, name, description, type, mcp_transport, mcp_endpoint, activity_name, input_schema, created_at, updated_at) VALUES ` +
        `('${skillId}', '${orgId}', '${skillName}', 'Returns the sum of two numbers', 'mcp', 'sse', 'http://mcp-everything:3001/sse', 'get-sum', ` +
        `'{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}', now(), now()); ` +
        `INSERT INTO access_grants (id, org_id, resource_type, resource_id, principal_type, principal_id, permission, created_via) ` +
        `VALUES ('${randomUUID()}', '${orgId}', 'skill', '${skillId}', 'organization', '${orgId}', 'read', 'provisioning');`
    );

    await page.getByRole("button", { name: "Choose model" }).click();
    await page.getByRole("option", { name: MODEL, exact: true }).click();

    let replyCount = 0;
    let fillerDone = 0;
    const fillerBudget = totalTurns - specialSegments;
    let conversationId: string | null = null;

    async function fillerTurn(phaseLabel: string): Promise<void> {
      await test.step(phaseLabel, async () => {
        const a = randomOperand();
        const b = randomOperand();
        const conversationCreated =
          conversationId === null
            ? page.waitForResponse(
                (response) =>
                  response.url() === `${BUSINESS_API}/v1/conversations` &&
                  response.request().method() === "POST"
              )
            : null;
        const { text, replyCount: nextCount } = await sendTurn(
          page,
          `What is ${a} plus ${b}? Answer with the number only, without words or explanations.`,
          replyCount,
          FILLER_TIMEOUT_MS
        );
        if (conversationCreated) {
          const created = await conversationCreated;
          conversationId = ((await created.json()).data as { id: string }).id;
        }
        expect(text, `${a}+${b}`).toMatch(new RegExp(`\\b${a + b}\\b`));
        replyCount = nextCount;
        fillerDone += 1;
      });
    }

    async function skillTurn(): Promise<void> {
      await test.step("skill: real MCP round trip", async () => {
        const a = randomOperand();
        const b = randomOperand();
        const { replyCount: nextCount } = await sendTurn(
          page,
          `What is ${a} plus ${b}? Use the available tool ` +
            `(skill_id: ${skillId}) to compute it, then answer with just the number.`,
          replyCount,
          SKILL_TIMEOUT_MS
        );
        const toolCall = page.locator(`[aria-label="${skillId}: Done"]`);
        await expect(toolCall).toBeVisible({ timeout: SKILL_TIMEOUT_MS });
        await expect(toolCall).toContainText('"isError":false');
        await expect(
          page.getByTestId("assistant-content").nth(nextCount - 1).getByText(new RegExp(`\\b${a + b}\\b`))
        ).toBeVisible();
        replyCount = nextCount;
      });
    }

    async function webSearchTurn(): Promise<void> {
      await test.step("web: real web_search + citation", async () => {
        if (!conversationId) throw new Error("conversation not yet created");
        const before = await getHistory(page, conversationId);
        const priorTurns = new Set(before.messages.data.map((m) => m.turnId));
        const { replyCount: nextCount } = await sendTurn(
          page,
          "Search the public web for: IANA reserved domains. Then answer in one short " +
            "sentence citing the used fact with an exact returned [source:UUID] marker.",
          replyCount,
          WEB_TIMEOUT_MS
        );
        replyCount = nextCount;
        const after = await getHistory(page, conversationId);
        const newAssistant = after.messages.data.find(
          (m) => m.role === "assistant" && m.turnId && !priorTurns.has(m.turnId)
        );
        expect(newAssistant?.turnId, "durable turn for the search reply").toBeTruthy();
        const sources = await apiAs<Source[]>(
          page,
          `/v1/conversations/${conversationId}/turns/${newAssistant!.turnId}/sources`
        );
        expect(sources.some((s) => s.kind === "search"), "real search produced a source").toBe(
          true
        );
      });
    }

    async function webReadTurn(): Promise<void> {
      await test.step("web: real web_read + citation", async () => {
        if (!conversationId) throw new Error("conversation not yet created");
        const before = await getHistory(page, conversationId);
        const priorTurns = new Set(before.messages.data.map((m) => m.turnId));
        const { replyCount: nextCount } = await sendTurn(
          page,
          "Read this page: https://www.iana.org/domains/reserved — then answer in one short " +
            "sentence what it is about, citing the fact with an exact returned [source:UUID] marker.",
          replyCount,
          WEB_TIMEOUT_MS
        );
        replyCount = nextCount;
        const after = await getHistory(page, conversationId);
        const newAssistant = after.messages.data.find(
          (m) => m.role === "assistant" && m.turnId && !priorTurns.has(m.turnId)
        );
        expect(newAssistant?.turnId, "durable turn for the read reply").toBeTruthy();
        const sources = await apiAs<Source[]>(
          page,
          `/v1/conversations/${conversationId}/turns/${newAssistant!.turnId}/sources`
        );
        expect(sources.some((s) => s.kind === "read"), "real read produced a source").toBe(true);
        expect(
          sources.every((s) => /^https:\/\/www\.iana\.org\//.test(s.finalUrl)),
          "safe provenance"
        ).toBe(true);
      });
    }

    async function questionTurn(): Promise<void> {
      await test.step("question: real model asks, user answers", async () => {
        if (!conversationId) throw new Error("conversation not yet created");
        const questionText = `Which build tool do you prefer, ${randomUUID().slice(0, 8)}?`;
        const outcome = sendTurn(
          page,
          "Before answering anything else, you must ask me a clarifying question using " +
            `the ask_user tool. Ask exactly this question: "${questionText}" with two ` +
            'options: id "uv" label "uv", and id "poetry" label "Poetry". Wait for my ' +
            "answer, then thank me by name of the tool I picked.",
          replyCount,
          QUESTION_ANSWER_TIMEOUT_MS
        ).then(
          (value) => ({ value }),
          (error) => ({ error })
        );

        await expect(page.getByText(questionText, { exact: true })).toBeVisible({
          timeout: QUESTION_VISIBLE_TIMEOUT_MS,
        });
        // The composer must refuse a new turn while this question is open —
        // the platform guarantee this segment is really here to check.
        await expect(page.getByLabel("Message")).toBeDisabled();
        await expect(page.getByLabel("Message")).toHaveAttribute(
          "placeholder",
          "Answer the question above to continue"
        );

        await page
          .locator('[aria-label="Answer options"]')
          .getByRole("button", { name: "uv", exact: true })
          .click();

        const result = await outcome;
        if ("error" in result) throw result.error;
        replyCount = result.value.replyCount;

        const questions = await apiAs<Question[]>(
          page,
          `/v1/conversations/${conversationId}/questions`
        );
        const answered = questions.find((q) => q.text === questionText);
        expect(answered?.status, "question durably answered").toBe("answered");
      });
    }

    // --- The actual dialog, in a natural-ish order rather than grouped by kind. ---
    await fillerTurn("filler: warm-up 1");
    await fillerTurn("filler: warm-up 2");
    await skillTurn();
    await fillerTurn("filler: after skill");
    if (WEB_ENABLED) {
      await webSearchTurn();
      await fillerTurn("filler: between web turns");
      await webReadTurn();
    }
    await fillerTurn("filler: before question");
    if (QUESTIONS_ENABLED) {
      await questionTurn();
    }
    while (fillerDone < fillerBudget) {
      await fillerTurn(`filler: ${fillerDone + 1}/${fillerBudget}`);
    }

    // --- Whole-conversation invariants only a long dialog can exercise. ---
    await test.step("final: durable history matches the live count", async () => {
      if (!conversationId) throw new Error("conversation was never created");
      const historyConversationId = conversationId;
      // The last turn's SSE "done" event can fire slightly before the outbox-driven
      // persist commits the durable row, so this poll tolerates that lag instead of
      // asserting immediately — every other check in this file waits on a Playwright
      // locator (which auto-retries); this is the one raw API-count comparison.
      let assistantMessages: History["messages"]["data"] = [];
      await expect
        .poll(
          async () => {
            const history = await getHistory(page, historyConversationId);
            assistantMessages = history.messages.data.filter((m) => m.role === "assistant");
            return assistantMessages.length;
          },
          { message: "one durable row per assistant reply", timeout: 15_000 }
        )
        .toBe(replyCount);
      const turnIds = assistantMessages.map((m) => m.turnId).filter(Boolean);
      expect(new Set(turnIds).size, "every reply has a distinct durable turn").toBe(
        assistantMessages.length
      );
      expect(await page.getByTestId("assistant-content").count()).toBe(replyCount);
      // Reload through the production UI loader too: a correct API-only
      // traversal must not hide a frontend that still takes just page one.
      await page.reload();
      await expect(page.getByTestId("assistant-content")).toHaveCount(replyCount);

    });
  });
});
