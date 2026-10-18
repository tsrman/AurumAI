import { randomUUID } from "node:crypto";

import { expect, type Locator, type Page, type Response } from "@playwright/test";
import { nextAssistantContent } from "./new-assistant";

type Fixture = (command: string, id?: string, action?: string) => string;
type Api = <T>(endpoint: string, method?: string, body?: unknown, expected?: number) => Promise<T>;
type Operation = {
  kind: "ask_user" | "web_search" | "web_read";
  attempts: number;
  status: string;
  error: string | null;
  phase: string;
  nodeKey: string | null;
};
type Proof = {
  status: string;
  questions: number;
  reads: number;
  searches: number;
  workflowRun: string | null;
  operations: Operation[];
};
type WorkflowRun = {
  id: string;
  conversationId: string;
  turnId: string;
  status: string;
  errorCode: string | null;
  nodes: { phaseKey: string; status: string; attemptCount: number }[];
};
type Source = { sourceId: string };
type Message = { id: string; role: string; content: string; turnId?: string; sourceIds?: string[] };
type History = { messages: { data: Message[] } };
type Question = {
  questionId: string;
  turnId: string;
  text: string;
  status: "pending" | "answered" | "expired" | "cancelled";
  nodeKey: string | null;
};

function parsedEvents(body: string): { type: string; data: Record<string, unknown> }[] {
  return body.split(/\r?\n\r?\n/).flatMap((block) => {
    const type = block.match(/^event: (.+)$/m)?.[1]?.trim();
    const raw = block.match(/^data: (.+)$/m)?.[1];
    if (!type || !raw) return [];
    try {
      const data = JSON.parse(raw) as unknown;
      return data && typeof data === "object"
        ? [{ type, data: data as Record<string, unknown> }]
        : [];
    } catch {
      return [];
    }
  });
}

function liveSourceIds(body: string, turnId: string): string[] {
  const content = parsedEvents(body).find((event) =>
    event.type === "message-end" && event.data.turn_id === turnId
  )?.data.content;
  if (typeof content !== "string") return [];
  return [...content.matchAll(/\[source:([0-9a-f-]{36})\]/gi)].map((match) => match[1]);
}

function sameIdentities(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const observed = new Set(left);
  const expected = new Set(right);
  return observed.size === left.length
    && expected.size === right.length
    && left.every((identity) => expected.has(identity));
}

export async function verifyInteractiveMultiAgent({
  page,
  conversation,
  workflowName,
  controlled,
  api,
  fixture,
  phase,
}: {
  page: Page;
  conversation: string;
  workflowName: string;
  controlled: boolean;
  api: Api;
  fixture: Fixture;
  phase: (value: string) => void;
}): Promise<void> {
  const proof = (turnId: string) => JSON.parse(fixture("turn-proof", turnId)) as Proof;
  const faults = (id: string, action: string) =>
    JSON.parse(fixture("fault", id, action)) as {
      attempts: number;
      finished: number;
      released: boolean;
    };

  async function selectWorkflow(): Promise<void> {
    await page.reload();
    await expect(page.getByLabel("Message")).toBeVisible();
    await page.getByRole("button", { name: "Choose workflow" }).click();
    await page.getByRole("option", { name: new RegExp(`^${workflowName}(?:\\s|$)`) }).click();
  }

  async function startTurn(prompt: string): Promise<{
    response: Response;
    assistant: Locator;
    priorTurns: Set<string | undefined>;
  }> {
    const history = await api<History>(`/v1/conversations/${conversation}`);
    const priorTurns = new Set(history.messages.data.map((message) => message.turnId));
    const assistant = await nextAssistantContent(page,
      history.messages.data.filter(message => message.role === "assistant").map(message => message.id));
    const pending = page.waitForResponse((response) =>
      response.url().endsWith("/v1/chat/stream") && response.request().method() === "POST"
    );
    await page.getByLabel("Message").fill(prompt);
    await page.getByLabel("Send").click();
    const response = await pending;
    expect(response.status(), "multi-agent stream accepted").toBe(200);
    expect(response.request().postDataJSON().workflowId, "explicit workflow picker wins").toBe(workflowId);
    return { response, assistant, priorTurns };
  }

  async function finishTurn(
    started: { response: Response; assistant: Locator; priorTurns: Set<string | undefined> },
    expected: string,
  ): Promise<{ turnId: string; message: Message; body: string }> {
    const body = await started.response.text();
    const events = parsedEvents(body);
    expect(events.some((event) => event.type === "error"), "workflow stream error absent").toBe(false);
    expect(events.some((event) => event.type === "done"), "workflow stream terminal event").toBe(true);
    await expect(started.assistant).toHaveCount(1, { timeout: 120_000 });
    await expect(started.assistant).toContainText(expected, {
      timeout: 120_000,
    });
    const history = await api<History>(`/v1/conversations/${conversation}`);
    const messages = history.messages.data.filter((item) =>
      item.role === "assistant" && item.turnId && !started.priorTurns.has(item.turnId)
    );
    expect(messages.length, "exactly one new durable workflow assistant").toBe(1);
    const message = messages[0];
    expect(Boolean(message?.turnId), "durable workflow turn identity").toBe(true);
    expect(message?.content.startsWith(expected), "durable workflow final marker").toBe(true);
    return { turnId: message!.turnId!, message: message!, body };
  }

  async function assertCompletedRun(turnId: string, runId: string): Promise<WorkflowRun> {
    const run = await api<WorkflowRun>(`/v1/workflow-runs/${runId}`);
    expect(run).toMatchObject({
      id: runId,
      conversationId: conversation,
      turnId,
      status: "completed",
      errorCode: null,
    });
    expect(Object.fromEntries(run.nodes.map((node) => [node.phaseKey, node.status]))).toMatchObject({
      alpha: "completed",
      beta: "completed",
      supervisor_synthesize: "completed",
    });
    return run;
  }

  phase("multi-agent-prepare");
  const prepared = JSON.parse(fixture("prepare-workflow")) as { workflowId?: string };
  const workflowId = prepared.workflowId;
  expect(workflowId).toMatch(/^[0-9a-f-]{36}$/i);

  fixture("update-workflow", undefined, "interactive");
  await selectWorkflow();
  const interactive = await startTurn("MULTI_AGENT_INTERACTIVE_WEB");
  phase("multi-agent-question-wait");
  await expect(page.getByText("Which multi-agent source topic?", { exact: true })).toBeVisible({
    timeout: 60_000,
  });
  const active = await api<{ turnId: string }>(
    `/v1/conversations/${conversation}/turns/active`,
  );
  const waitingProof = proof(active.turnId);
  expect(waitingProof).toMatchObject({ status: "active", questions: 1 });
  expect(waitingProof.workflowRun).toMatch(/^[0-9a-f-]{36}$/i);
  expect(waitingProof.operations).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ kind: "ask_user", phase: "node", nodeKey: "alpha" }),
    ]),
  );
  await expect.poll(async () => {
    const run = await api<WorkflowRun>(`/v1/workflow-runs/${waitingProof.workflowRun}`);
    return Object.fromEntries(run.nodes.map((node) => [node.phaseKey, node.status]));
  }, {
    timeout: 90_000,
    message: "web sibling completes while question node waits",
  }).toMatchObject({ alpha: "running", beta: "completed" });
  await page.getByRole("textbox", { name: "Answer" }).fill("IANA");
  await page.getByRole("button", { name: "Answer", exact: true }).click();

  phase("multi-agent-interactive-final");
  const interactiveFinal = await finishTurn(
    interactive,
    "MULTI_AGENT_WEB_FINAL:alpha,beta ANSWER:IANA",
  );
  expect(interactiveFinal.turnId).toBe(active.turnId);
  const interactiveProof = proof(interactiveFinal.turnId);
  expect(interactiveProof).toMatchObject({
    status: "completed",
    questions: 1,
    searches: 2,
    reads: 2,
    workflowRun: waitingProof.workflowRun,
  });
  for (const nodeKey of ["alpha", "beta"]) {
    expect(interactiveProof.operations).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "web_search", status: "completed", nodeKey }),
      expect.objectContaining({ kind: "web_read", status: "completed", nodeKey }),
    ]));
  }
  await assertCompletedRun(interactiveFinal.turnId, waitingProof.workflowRun!);
  const interactiveSources = await api<Source[]>(
    `/v1/conversations/${conversation}/turns/${interactiveFinal.turnId}/sources`,
  );
  expect(interactiveSources.length).toBeGreaterThanOrEqual(4);
  const expectedSourceIds = interactiveSources.map((source) => source.sourceId);
  expect(
    sameIdentities(liveSourceIds(interactiveFinal.body, interactiveFinal.turnId), expectedSourceIds),
    "live SSE source identities match durable sources",
  ).toBe(true);
  expect(
    sameIdentities(interactiveFinal.message.sourceIds || [], expectedSourceIds),
    "durable final source identities match owned sources",
  ).toBe(true);

  phase("multi-agent-question-budget");
  fixture("update-workflow", undefined, "question-budget");
  await selectWorkflow();
  const questionBudget = await startTurn("MULTI_AGENT_SHARED_QUESTION_BUDGET");
  const questionTurn = await api<{ turnId: string }>(
    `/v1/conversations/${conversation}/turns/active`,
  );
  expect(Boolean(questionTurn.turnId), "question budget active turn identity").toBe(true);
  phase("multi-agent-question-budget-answer");
  const answered = new Set<string>();
  for (let index = 0; index < 10; index++) {
    let pending: Question | undefined;
    await expect.poll(async () => {
      const questions = await api<Question[]>(`/v1/conversations/${conversation}/questions`);
      pending = questions.find((question) =>
        question.turnId === questionTurn.turnId
        && question.status === "pending"
        && !answered.has(question.questionId)
      );
      return pending !== undefined;
    }, {
      timeout: 60_000,
      message: "next owned multi-agent question becomes pending",
    }).toBe(true);
    const question = pending!;
    const card = page
      .getByRole("heading", { name: question.text, exact: true })
      .locator("xpath=ancestor::article[1]");
    await expect(card).toHaveCount(1, { timeout: 30_000 });
    const accepted = page.waitForResponse((response) =>
      response.url().endsWith(`/questions/${question.questionId}/answer`)
      && response.request().method() === "POST"
    );
    await card.getByRole("textbox", { name: "Answer" }).fill(`accepted-${index + 1}`);
    await card.getByRole("button", { name: "Answer", exact: true }).click();
    expect((await accepted).status(), "multi-agent answer accepted through UI").toBe(200);
    answered.add(question.questionId);
  }
  phase("multi-agent-question-budget-final");
  const questionFinal = await finishTurn(
    questionBudget,
    "MULTI_AGENT_QUESTION_BUDGET_FINAL:user_input_limit",
  );
  expect(questionFinal.turnId).toBe(questionTurn.turnId);
  const questionProof = proof(questionFinal.turnId);
  expect(questionProof).toMatchObject({
    status: "completed",
    questions: 10,
    reads: 0,
    searches: 0,
  });
  const questionOperations = questionProof.operations.filter(
    (operation) => operation.kind === "ask_user",
  );
  expect(questionOperations).toHaveLength(10);
  expect(new Set(questionOperations.map((operation) => operation.nodeKey))).toEqual(
    new Set(["alpha", "beta"]),
  );
  expect(questionOperations.every((operation) => operation.status === "completed")).toBe(true);
  const durableQuestions = (await api<Question[]>(
    `/v1/conversations/${conversation}/questions`,
  )).filter((question) => question.turnId === questionFinal.turnId);
  expect(durableQuestions).toHaveLength(10);
  expect(durableQuestions.every((question) => question.status === "answered")).toBe(true);
  // AgentWorkflow deliberately feeds user_input_limit back to the model as a
  // recoverable tool result, so this exact scenario completes with an honest
  // limit marker instead of creating a failed run or a false success marker.
  await assertCompletedRun(questionFinal.turnId, questionProof.workflowRun!);

  phase("multi-agent-budget");
  fixture("update-workflow", undefined, "budget");
  await selectWorkflow();
  const budgetFinal = await finishTurn(
    await startTurn("MULTI_AGENT_SHARED_BUDGET"),
    "MULTI_AGENT_BUDGET_FINAL:web_limit",
  );
  const budgetProof = proof(budgetFinal.turnId);
  expect(budgetFinal.turnId === questionFinal.turnId, "subsequent turn has new identity").toBe(false);
  expect(budgetProof).toMatchObject({
    status: "completed",
    questions: 0,
    searches: 10,
    reads: 0,
  });
  const searches = budgetProof.operations.filter((operation) => operation.kind === "web_search");
  expect(searches).toHaveLength(10);
  expect(new Set(searches.map((operation) => operation.nodeKey))).toEqual(new Set(["alpha", "beta"]));
  expect(searches.every((operation) => operation.status === "completed")).toBe(true);
  await assertCompletedRun(budgetFinal.turnId, budgetProof.workflowRun!);

  if (!controlled) return;

  phase("multi-agent-revoke-held");
  const faultId = randomUUID();
  faults(faultId, "arm/barrier");
  fixture("update-workflow", faultId, "revoke");
  await selectWorkflow();
  const revoked = await startTurn("MULTI_AGENT_HELD_REVOKE");
  let revokeTurnId = "";
  let permissionRevoked = false;
  try {
    await expect.poll(() => faults(faultId, "status").attempts, {
      timeout: 60_000,
      message: "owned upstream request reached held fixture",
    }).toBe(1);
    const held = await api<{ turnId: string }>(
      `/v1/conversations/${conversation}/turns/active`,
    );
    revokeTurnId = held.turnId;
    expect(proof(held.turnId)).toMatchObject({ status: "active", reads: 1 });
    fixture("revoke");
    permissionRevoked = true;
    expect(faults(faultId, "status").released).toBe(false);
    faults(faultId, "release");
    const revokeFinal = await finishTurn(revoked, "MULTI_AGENT_REVOKE_FINAL:web_access_denied");
    expect(revokeFinal.turnId).toBe(revokeTurnId);
    const revokeProof = proof(revokeFinal.turnId);
    expect(revokeProof).toMatchObject({ status: "completed", reads: 1, searches: 0 });
    expect(revokeProof.operations).toEqual(expect.arrayContaining([
      expect.objectContaining({
        kind: "web_read",
        status: "failed",
        error: "web_access_denied",
        nodeKey: "beta",
      }),
    ]));
    expect(faults(faultId, "status")).toMatchObject({ attempts: 1, finished: 1 });
    expect(await api<Source[]>(
      `/v1/conversations/${conversation}/turns/${revokeFinal.turnId}/sources`,
    )).toHaveLength(0);
    await assertCompletedRun(revokeFinal.turnId, revokeProof.workflowRun!);
  } finally {
    try {
      faults(faultId, "release");
    } finally {
      if (permissionRevoked) fixture("grant");
    }
  }
}
