import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { breakFirstTurnStream } from "./turn-stream-fault";
import { nextAssistantContent } from "./new-assistant";

const ENABLED = process.env.E2E_USER_QUESTIONS === "true";
const STATE = process.env.E2E_USER_QUESTIONS_STATE || "";
const MARKER = process.env.E2E_USER_QUESTIONS_MARKER || "";
const BUSINESS = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const BUSINESS_API =
  process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
const ROOT = path.resolve(__dirname, "../..");
const MODEL = `${MARKER}-model`;
const KIND = process.env.QUESTION_ACCEPTANCE_KIND || "short";
const IS_KUBERNETES = Boolean(process.env.E2E_USER_QUESTIONS_FIXTURE_SCRIPT);
const ANSWER_ACTION_TIMEOUT = 30_000;
const ANSWER_RESPONSE_TIMEOUT = 90_000;
const PENDING_QUESTION_TIMEOUT = 90_000;
const FIXTURE_SCRIPT =
  process.env.E2E_USER_QUESTIONS_FIXTURE_SCRIPT ||
  path.join(ROOT, "scripts/user_questions_e2e.py");
const FIXTURE_PYTHON = process.env.E2E_USER_QUESTIONS_PYTHON || "python3";

type Fixture = { marker: string; member_id: string };
type Question = {
  questionId: string;
  turnId: string;
  text: string;
  status: "pending" | "answered" | "expired" | "cancelled";
  answer: { text?: string; optionId?: string } | null;
};

function record(kind: "conversation" | "question", id: string) {
  execFileSync(
    FIXTURE_PYTHON,
    [
      FIXTURE_SCRIPT,
      "record",
      "--state",
      STATE,
      "--marker",
      MARKER,
      "--kind",
      kind,
      "--id",
      id,
    ],
    { cwd: ROOT, env: process.env, stdio: "inherit" }
  );
}

function fixtureAction(
  action: "stop-worker" | "start-worker" | "defer-delivery",
  flag: "--conversation" | "--question",
  id: string,
  extra: string[] = []
) {
  execFileSync(
    FIXTURE_PYTHON,
    [
      FIXTURE_SCRIPT,
      action,
      "--state",
      STATE,
      "--marker",
      MARKER,
      flag,
      id,
      ...extra,
    ],
    { cwd: ROOT, env: process.env, stdio: "inherit" }
  );
}

async function login(page: Page) {
  await page.goto(`${BUSINESS}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.locator("#username").fill(process.env.E2E_MEMBER_USERNAME || "user");
  await page.locator("#password").fill(process.env.E2E_MEMBER_PASSWORD || "user");
  await page.locator("#kc-login").click();
  await page.waitForURL(`${BUSINESS}/chat`);
  await expect.poll(() => page.evaluate(() => Boolean(window.keycloak?.token))).toBe(true);
}

async function api<T>(
  page: Page,
  endpoint: string,
  method = "GET",
  body?: unknown,
  expectedStatus = 200
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
      const payload = await response.json().catch(() => null);
      return { status: response.status, data: payload?.data };
    },
    { url: `${BUSINESS_API}${endpoint}`, method, body }
  );
  expect(result.status, `${method} ${endpoint}`).toBe(expectedStatus);
  return result.data as T;
}

async function questions(page: Page, conversationId: string): Promise<Question[]> {
  return api<Question[]>(page, `/v1/conversations/${conversationId}/questions`);
}

async function pendingQuestion(
  page: Page,
  conversationId: string,
  text: string
): Promise<Question> {
  let found: Question | undefined;
  await expect
    .poll(
      async () => {
        found = (await questions(page, conversationId)).find(
          (question) => question.status === "pending" && question.text === text
        );
        return found?.questionId;
      },
      { timeout: PENDING_QUESTION_TIMEOUT, intervals: [500] }
    )
    .toMatch(/^[0-9a-f-]{36}$/i);
  return found as Question;
}

async function answerTextQuestion(
  page: Page,
  questionId: string,
  value: string
): Promise<void> {
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/questions/${questionId}/answer`) &&
      response.request().method() === "POST",
    { timeout: ANSWER_RESPONSE_TIMEOUT }
  );
  await page.getByRole("textbox", { name: "Answer", exact: true }).fill(value);
  await page
    .getByRole("button", { name: "Answer", exact: true })
    .click({ timeout: ANSWER_ACTION_TIMEOUT });
  expect((await accepted).status()).toBe(200);
}

test.use({ trace: "off", video: "off", screenshot: "off" });
test.skip(!ENABLED, "requires exclusive durable user-question launcher");

test("durable questions survive two tabs, reload and two rounds", async ({ browser }) => {
  // Production still owns the exact 3600-second DB deadline. The larger
  // Playwright budget only absorbs known WSL host/container monotonic-clock
  // drift, so the harness cannot cancel the turn before PostgreSQL expires it.
  test.setTimeout(
    KIND === "hour" ? 4_800_000 : IS_KUBERNETES ? 600_000 : 300_000
  );
  expect(MARKER).toMatch(/^user-questions-e2e-[a-f0-9]{16}$/);
  expect(["short", "restart", "hour"]).toContain(KIND);
  const fixture = JSON.parse(readFileSync(STATE, "utf8")) as Fixture;
  expect(fixture.marker).toBe(MARKER);

  const firstContext = await browser.newContext();
  const secondContext = await browser.newContext();
  try {
    const first = await firstContext.newPage();
    const second = await secondContext.newPage();
    async function nextKnownAssistant(conversationId: string) {
      const history = await api<{ messages: { data: { id: string; role: string }[] } }>(
        first, `/v1/conversations/${conversationId}`);
      return nextAssistantContent(first,
        history.messages.data.filter(message => message.role === "assistant").map(message => message.id));
    }
    await login(first);
    await first.getByRole("button", { name: "Choose model" }).click();
    await first
      .getByRole("option", { name: new RegExp(`^${MODEL}(?:\\s|$)`) })
      .click();

    const firstPrompt = `${MARKER} ASK_CITY_THEN_FINAL`;
    const firstAssistant = await nextAssistantContent(first);
    const conversationCreated = first.waitForResponse(
      (response) =>
        response.url() === `${BUSINESS_API}/v1/conversations` &&
        response.request().method() === "POST"
    );
    const firstStreamPending = first.waitForResponse(
      (response) =>
        response.url() === `${BUSINESS_API}/v1/chat/stream` &&
        response.request().method() === "POST"
    );
    await first.getByLabel("Message").fill(firstPrompt);
    await first.getByLabel("Send").click();
    const creation = await conversationCreated;
    expect(creation.status()).toBe(200);
    const conversationId = (await creation.json()).data.id as string;
    record("conversation", conversationId);
    const firstStream = await firstStreamPending;
    expect(firstStream.status()).toBe(200);

    await expect(first.getByText("Which city?", { exact: true })).toBeVisible({
      timeout: 90_000,
    });
    const cityQuestion = await pendingQuestion(first, conversationId, "Which city?");
    record("question", cityQuestion.questionId);
    await expect(first.getByLabel("Message")).toBeDisabled();
    await expect(first.getByLabel("Message")).toHaveAttribute(
      "placeholder",
      "Answer the question above to continue"
    );

    if (KIND === "hour") {
      await expect
        .poll(
          async () =>
            (await questions(first, conversationId)).find(
              (question) => question.questionId === cityQuestion.questionId
            )?.status,
          { timeout: 4_500_000, intervals: [5_000] }
        )
        .toBe("expired");
      await api<unknown>(
        first,
        `/v1/conversations/${conversationId}/questions/${cityQuestion.questionId}/answer`,
        "POST",
        {
          answer: { text: "too late" },
          idempotencyKey: "10000000-0000-4000-8000-000000000099",
        },
        409
      );
      await expect(first.getByLabel("Message")).toBeEnabled({ timeout: 90_000 });
      const timeoutAssistant = await nextKnownAssistant(conversationId);
      await first.getByLabel("Message").fill("POST_TIMEOUT_NEXT_TURN");
      await first.getByLabel("Send").click();
      await expect(timeoutAssistant).toHaveText(
        "Hello from the deterministic Kubernetes gateway.",
        { timeout: 90_000 }
      );
      return;
    }

    const conflict = await api<unknown>(
      first,
      "/v1/chat/stream",
      "POST",
      {
        conversation_id: conversationId,
        messages: [{ role: "user", content: "must not start" }],
        model: MODEL,
        options: {},
      },
      409
    );
    expect(conflict).toBeUndefined();

    // The observer must recover a broken response body without navigation,
    // refocus or manual query invalidation when the other tab answers.
    await breakFirstTurnStream(second);
    let observerStreams = 0;
    second.on("response", response => {
      if (/\/turns\/[^/]+\/events$/.test(response.url()) && response.ok()) observerStreams++;
    });
    await login(second);
    await second.goto(`${BUSINESS}/history`);
    const title = firstPrompt.slice(0, 60);
    await second
      .getByRole("main")
      .getByRole("button", { name: `Open conversation ${title}` })
      .click();
    await second.waitForURL(`${BUSINESS}/chat`);
    await expect(second.getByText("Which city?", { exact: true })).toBeVisible();
    await expect.poll(() => observerStreams, { timeout: 30_000 }).toBeGreaterThanOrEqual(2);

    if (KIND === "restart") {
      fixtureAction("stop-worker", "--conversation", conversationId);
    }
    await answerTextQuestion(first, cityQuestion.questionId, "Kazan");
    if (KIND === "restart") {
      fixtureAction(
        "defer-delivery",
        "--question",
        cityQuestion.questionId,
        ["--seconds", "10"]
      );
      fixtureAction("start-worker", "--conversation", conversationId);
    }
    await expect(second.getByText("Kazan", { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    await expect(firstAssistant).toHaveText(
      "CITY CONFIRMED: Kazan",
      { timeout: 90_000 }
    );
    expect(await firstStream.text()).toMatch(/^event: done\r?$/m);

    await first.reload();
    await expect(first.getByText("Kazan", { exact: true })).toBeVisible();
    await expect(first.getByText(/Answered/).first()).toBeVisible();
    await expect(first.getByRole("textbox", { name: "Answer", exact: true })).toHaveCount(0);
    await expect(first.getByLabel("Message")).toBeEnabled();

    const twiceAssistant = await nextKnownAssistant(conversationId);
    const twiceStreamPending = first.waitForResponse(
      (response) =>
        response.url() === `${BUSINESS_API}/v1/chat/stream` &&
        response.request().method() === "POST"
    );
    await first.getByLabel("Message").fill("ASK_TWICE");
    await first.getByLabel("Send").click();
    const twiceStream = await twiceStreamPending;
    expect(twiceStream.status()).toBe(200);

    const twiceCity = await pendingQuestion(first, conversationId, "Which city?");
    record("question", twiceCity.questionId);
    await answerTextQuestion(first, twiceCity.questionId, "Kazan");
    const district = await pendingQuestion(first, conversationId, "Which district?");
    record("question", district.questionId);
    await answerTextQuestion(first, district.questionId, "Vakhitovsky");

    const twiceBody = await twiceStream.text();
    expect(twiceBody.includes("LOCATION CONFIRMED: Kazan / Vakhitovsky"),
      "two-round backend final marker").toBe(true);
    expect(twiceBody).toMatch(/^event: done\r?$/m);
    await expect(twiceAssistant).toHaveText(
      "LOCATION CONFIRMED: Kazan / Vakhitovsky",
      { timeout: 90_000 }
    );
    await expect
      .poll(async () => (await questions(first, conversationId)).filter(
        (question) => question.status === "answered"
      ).length)
      .toBe(3);

    async function send(prompt: string) {
      await expect(first.getByLabel("Message")).toBeEnabled({ timeout: 90_000 });
      const assistant = await nextKnownAssistant(conversationId);
      const response = first.waitForResponse(r => r.url() === `${BUSINESS_API}/v1/chat/stream`
        && r.request().method() === "POST");
      await first.getByLabel("Message").fill(prompt);
      await first.getByLabel("Send").click();
      const admitted = await response;
      expect(admitted.status(), "next turn admission").toBe(200);
      return { response: admitted, assistant };
    }
    function access(action: "revoke" | "grant") {
      try {
        execFileSync(FIXTURE_PYTHON, [FIXTURE_SCRIPT, action, "--state", STATE, "--marker", MARKER],
          { cwd: ROOT, env: process.env, stdio: ["ignore", "pipe", "pipe"] });
      } catch { throw new Error("owned model access change failed; preserve journal"); }
    }

    const optionTurn = await send("ASK_OPTION");
    const option = await pendingQuestion(first, conversationId, "Choose city?");
    record("question", option.questionId);
    const selected = first.waitForResponse(r => r.url().endsWith(`/questions/${option.questionId}/answer`)
      && r.request().method() === "POST");
    await first.getByRole("button", { name: "Tomsk", exact: true }).click();
    expect((await selected).status()).toBe(200);
    await expect(optionTurn.assistant).toHaveText("OPTION CONFIRMED: tomsk", { timeout: 90_000 });
    expect((await questions(first, conversationId)).find(q => q.questionId === option.questionId)?.answer).toEqual({ optionId: "tomsk", text: null });

    const raceTurn = await send("ASK_CITY_THEN_FINAL");
    const race = await pendingQuestion(first, conversationId, "Which city?");
    record("question", race.questionId);
    await second.reload();
    await expect(second.getByRole("textbox", { name: "Answer", exact: true })).toBeVisible();
    // Hold only the browser's outgoing requests until BOTH real UI submits
    // exist. Responses are never mocked; PostgreSQL decides the winner.
    const answerUrl = `${BUSINESS_API}/v1/conversations/${conversationId}/questions/${race.questionId}/answer`;
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    let arrived = 0;
    for (const page of [first, second]) {
      await page.route(answerUrl, async route => {
        if (route.request().method() !== "POST") {
          await route.continue();
          return;
        }
        arrived++;
        if (arrived === 2) release();
        await gate;
        await route.continue();
      });
    }
    try {
      await first.getByRole("textbox", { name: "Answer", exact: true }).fill("Kazan race");
      await second.getByRole("textbox", { name: "Answer", exact: true }).fill("Tomsk race");
      const replies = [first, second].map(page => page.waitForResponse(response =>
        response.url() === answerUrl && response.request().method() === "POST"));
      await Promise.all([first, second].map(page => page.getByRole("button", { name: "Answer", exact: true }).click()));
      expect((await Promise.all(replies)).map(r => r.status()).sort()).toEqual([200, 409]);
    } finally {
      release();
      await Promise.all([first, second].map(page => page.unroute(answerUrl)));
    }
    const winner = (await questions(first, conversationId)).find(q => q.questionId === race.questionId)?.answer?.text;
    expect(["Kazan race", "Tomsk race"]).toContain(winner);
    await expect(raceTurn.assistant).toHaveText(`CITY CONFIRMED: ${winner}`, { timeout: 90_000 });
    await expect(second.getByText(winner!, { exact: true })).toBeVisible();

    await send("ASK_CITY_THEN_FINAL");
    const revoked = await pendingQuestion(first, conversationId, "Which city?");
    record("question", revoked.questionId);
    try {
      access("revoke");
      await api(first, `/v1/conversations/${conversationId}/questions/${revoked.questionId}/answer`, "POST",
        { answer: { text: "must not be consumed" }, idempotencyKey: "20000000-0000-4000-8000-000000000099" }, 403);
      const unchanged = (await questions(first, conversationId)).find(q => q.questionId === revoked.questionId);
      expect(unchanged?.answer).toBeNull();
    } finally { access("grant"); }
    await first.getByLabel("Stop generating").click();
    await expect.poll(async () => (await questions(first, conversationId)).find(q => q.questionId === revoked.questionId)?.status,
      { timeout: 30_000 }).toBe("cancelled");
    await api(first, `/v1/conversations/${conversationId}/questions/${revoked.questionId}/answer`, "POST",
      { answer: { text: "late" }, idempotencyKey: "30000000-0000-4000-8000-000000000099" }, 409);
    await expect.poll(() => api(first, `/v1/conversations/${conversationId}/turns/active`),
      { timeout: 30_000, message: "cancel closes the exact durable turn before next admission" }).toBeNull();
    const nextTurn = await send("AFTER_CANCEL_NEXT_TURN");
    const nextStream = await nextTurn.response.text();
    expect(nextStream.includes("Hello from the deterministic Kubernetes gateway."), "next turn backend final marker").toBe(true);
    await expect(nextTurn.assistant).toHaveText("Hello from the deterministic Kubernetes gateway.", { timeout: 90_000 });
    const history = await api<{ messages: { data: { role: string; turnId?: string; content: string }[] } }>(first, `/v1/conversations/${conversationId}`);
    expect(history.messages.data.filter(m => m.role === "assistant" && m.turnId === revoked.turnId)).toHaveLength(0);
    expect(history.messages.data.filter(m => m.role === "assistant" && m.turnId === option.turnId)
      .map(m => m.content)).toEqual(["OPTION CONFIRMED: tomsk"]);
    expect(history.messages.data.filter(m => m.role === "assistant" && m.content === `CITY CONFIRMED: ${winner}`)).toHaveLength(1);
  } finally {
    await Promise.allSettled([firstContext.close(), secondContext.close()]);
  }
});
