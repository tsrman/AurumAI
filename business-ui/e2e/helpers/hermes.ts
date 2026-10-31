import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

/** Shared by the Hermes Agent browser suites (hermes.real*.spec.ts). */
export const BUSINESS_URL = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
export const ADMIN_UI_URL = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";
const ADMIN_API = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const KEYCLOAK_URL = process.env.E2E_KEYCLOAK_URL || "http://localhost:8080";

type Grant = { principalType: string; principalId: string; permission: string };
type HermesSettings = { egressMode: string; model: string | null };

export async function loginAt(page: Page, appUrl: string, landing: RegExp, username: string): Promise<void> {
  await page.goto(`${appUrl}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(username);
  await page.locator("#kc-login").click();
  await page.waitForURL(landing, { timeout: 30_000 });
}

export async function adminToken(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${KEYCLOAK_URL}/realms/aurum-ai/protocol/openid-connect/token`, {
    form: { grant_type: "password", client_id: "admin-ui", username: "admin", password: "admin" },
  });
  expect(response.ok()).toBeTruthy();
  return ((await response.json()) as { access_token: string }).access_token;
}

export async function adminApi<T>(
  request: APIRequestContext,
  token: string,
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  data?: unknown,
  status = 200
): Promise<T> {
  const response = await request.fetch(`${ADMIN_API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  expect(response.status(), `${method} ${path}`).toBe(status);
  return status === 204 ? (undefined as T) : ((await response.json()) as { data: T }).data;
}

/**
 * Lets the organization use Hermes on `model`, starting with no Hermes model
 * so an Admin UI choice is a real change; returns what puts everything back.
 */
export async function enableHermes(
  request: APIRequestContext,
  token: string,
  model: string
): Promise<() => Promise<void>> {
  const subjects = await adminApi<{ principalType: string; principalId: string }[]>(
    request, token, "GET", "/v1/admin/authorization/subjects"
  );
  const organization = subjects.find((subject) => subject.principalType === "organization");
  expect(organization).toBeDefined();
  const capabilities = (
    await adminApi<{ capabilities: string[] }>(request, token, "GET", "/v1/admin/authorization/capabilities/organization")
  ).capabilities;
  const grants = (
    await adminApi<Grant[]>(
      request, token, "GET", `/v1/admin/authorization/grants?resourceType=model&resourceId=${encodeURIComponent(model)}`
    )
  ).map(({ principalType, principalId, permission }) => ({ principalType, principalId, permission }));
  const hermes = await adminApi<HermesSettings>(request, token, "GET", "/v1/admin/settings/hermes");

  await adminApi(request, token, "PUT", "/v1/admin/authorization/capabilities/organization", {
    capabilities: [...new Set([...capabilities, "chat.send", "agents.hermes"])],
  });
  await adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
    resourceType: "model",
    resourceId: model,
    grants: [
      ...grants.filter((grant) => grant.principalType !== "organization"),
      { principalType: "organization", principalId: organization!.principalId, permission: "read" },
    ],
  });
  await adminApi(request, token, "PUT", "/v1/admin/settings/hermes", { model: null, egressMode: "none" });

  return async () => {
    // A real model's run outlasts the admin token's few minutes.
    token = await adminToken(request);
    await adminApi(request, token, "PUT", "/v1/admin/settings/hermes", {
      model: hermes.model,
      egressMode: hermes.egressMode,
    });
    await adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
      resourceType: "model",
      resourceId: model,
      grants,
    });
    await adminApi(request, token, "PUT", "/v1/admin/authorization/capabilities/organization", { capabilities });
  };
}

/** Picks the organization's Hermes model on the Admin UI's Models page. */
export async function chooseHermesModel(admin: Page, model: string): Promise<void> {
  await admin.goto(`${ADMIN_UI_URL}/admin/models`);
  const hermes = section(admin, "Hermes Agent");
  await hermes.getByLabel("Model", { exact: true }).selectOption(model);
  await hermes.getByLabel("Internet access").selectOption("none");
  const saved = admin.waitForResponse(
    (response) =>
      response.url().endsWith("/v1/admin/settings/hermes") &&
      response.request().method() === "PUT" &&
      response.status() === 200
  );
  await hermes.getByRole("button", { name: "Save Hermes Agent settings" }).click();
  await saved;
}

export async function chooseHermes(member: Page): Promise<void> {
  await member.getByRole("button", { name: "Choose workflow" }).click();
  await member.getByRole("option", { name: /^Hermes Agent/ }).click();
}

/** Sends a message and waits for the answer that follows it. */
/** The organization's default Hermes network mode, for package installs; enableHermes's restore undoes it. */
export async function allowPackages(request: APIRequestContext): Promise<void> {
  const token = await adminToken(request);
  const current = await adminApi<HermesSettings>(request, token, "GET", "/v1/admin/settings/hermes");
  await adminApi(request, token, "PUT", "/v1/admin/settings/hermes", { model: current.model, egressMode: "packages" });
}

type ImageSettings = { generationModel: string | null; editModel: string | null; visionModels: string[] };

/** Mark the model as taking images; the returned function restores the organization's list. */
export async function markVision(request: APIRequestContext, model: string): Promise<() => Promise<void>> {
  const token = await adminToken(request);
  const current = await adminApi<ImageSettings>(request, token, "GET", "/v1/admin/settings/images");
  const update = (visionModels: string[]) => ({
    generationModel: current.generationModel,
    editModel: current.editModel,
    visionModels,
  });
  await adminApi(request, token, "PUT", "/v1/admin/settings/images", update([...new Set([...current.visionModels, model])]));
  return async () => {
    await adminApi(request, await adminToken(request), "PUT", "/v1/admin/settings/images", update(current.visionModels));
  };
}

export async function ask(page: Page, text: string, expected: string | RegExp, timeout: number): Promise<void> {
  const replies = page.getByTestId("assistant-content");
  const before = await replies.count();
  await expect(page.getByLabel("Message")).toBeEnabled({ timeout });
  await page.getByLabel("Message").fill(text);
  await page.getByLabel("Send").click();
  await expect(replies).toHaveCount(before + 1, { timeout });
  await expect(replies.last()).toContainText(/\S/u, { timeout });
  // A failed turn ends with its reason: report it now instead of waiting out the timeout.
  const reply = (await replies.last().textContent()) ?? "";
  expect(reply, "the turn failed").not.toContain(TURN_FAILED);
  await expect(replies.last()).toContainText(expected, { timeout });
}

/** How the runner ends a failed turn (conversation-runner's _fail_turn). */
export const TURN_FAILED = "Could not get a model response";

export function section(page: Page, title: string): Locator {
  return page.getByRole("heading", { name: title, exact: true }).locator("xpath=ancestor::section[1]");
}

// One reused pair of agents and one workflow, so repeated runs do not pile up
// definitions. Members see them only while a test grants them.
const PLANNER = "Hermes e2e planner";
const BUILDER = "Hermes e2e builder";
const WORKFLOW = "Hermes e2e workflow";

type Grant2 = { principalType: string; principalId: string; permission: string };

/**
 * A workflow whose supervisor is an ordinary agent and whose one node,
 * `build`, runs on a Hermes Agent definition; granted to the organization
 * until the returned restore runs.
 */
export async function hermesWorkflow(
  request: APIRequestContext,
  token: string,
  model: string,
  instruction: string
): Promise<{ name: string; restore: () => Promise<void> }> {
  const agents = await adminApi<{ id: string; name: string }[]>(request, token, "GET", "/v1/agent-definitions");
  const agentId = async (name: string, kind: "agent" | "hermes") => {
    const existing = agents.find((agent) => agent.name === name)?.id;
    if (existing) {
      // Another run (the deterministic or the real-model one) may have left another model.
      await adminApi(request, token, "PATCH", `/v1/agent-definitions/${existing}`, {
        modelId: model,
        ...(kind === "agent"
          ? { systemPrompt: "Plan and synthesize. Never ask the user questions: the request is complete as given." }
          : {}),
      });
      return existing;
    }
    return (
      await adminApi<{ id: string }>(request, token, "POST", "/v1/agent-definitions", {
        name,
        modelId: model,
        systemPrompt:
          kind === "hermes"
            ? "Do the work in your workspace."
            : "Plan and synthesize. Never ask the user questions: the request is complete as given.",
        temperature: 0,
        kind,
      }, 201)
    ).id;
  };
  const plannerId = await agentId(PLANNER, "agent");
  const builderId = await agentId(BUILDER, "hermes");
  const draft = {
    name: WORKFLOW,
    description: "Hermes acceptance: a Hermes Agent runs the only node.",
    supervisorAgentDefinitionId: plannerId,
    autoRoutable: false,
    maxParallelism: 1,
    nodes: [
      { nodeKey: "build", displayName: "Build", agentDefinitionId: builderId, instruction, requiresConfirmation: false },
    ],
    edges: [],
  };
  const workflows = await adminApi<{ id: string; name: string }[]>(request, token, "GET", "/v1/admin/workflows");
  let workflowId = workflows.find((workflow) => workflow.name === WORKFLOW)?.id;
  if (!workflowId) {
    workflowId = (await adminApi<{ id: string }>(request, token, "POST", "/v1/admin/workflows", draft, 201)).id;
  } else {
    const current = await adminApi<{ version: number }>(request, token, "GET", `/v1/admin/workflows/${workflowId}`);
    await adminApi(request, token, "PUT", `/v1/admin/workflows/${workflowId}`, { ...draft, version: current.version });
  }
  const subjects = await adminApi<{ principalType: string; principalId: string }[]>(
    request, token, "GET", "/v1/admin/authorization/subjects"
  );
  const organization = subjects.find((subject) => subject.principalType === "organization")!;
  const originals: { resourceType: string; resourceId: string; grants: Grant2[] }[] = [];
  for (const [resourceType, resourceId] of [
    ["workflow", workflowId],
    ["agent_definition", plannerId],
    ["agent_definition", builderId],
  ]) {
    const grants = (
      await adminApi<Grant2[]>(
        request, token, "GET", `/v1/admin/authorization/grants?resourceType=${resourceType}&resourceId=${resourceId}`
      )
    ).map(({ principalType, principalId, permission }) => ({ principalType, principalId, permission }));
    originals.push({ resourceType, resourceId, grants });
    await adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
      resourceType,
      resourceId,
      grants: [
        ...grants.filter((grant) => grant.principalType !== "organization"),
        { principalType: "organization", principalId: organization.principalId, permission: "read" },
      ],
    });
  }
  return {
    name: WORKFLOW,
    restore: async () => {
      const fresh = await adminToken(request);
      for (const original of originals) {
        await adminApi(request, fresh, "PUT", "/v1/admin/authorization/grants/reconcile", original);
      }
    },
  };
}
