import {
  expect,
  test,
  type APIRequestContext,
  type BrowserContext,
  type Page,
} from "@playwright/test";

/**
 * Image generation, editing and attachments end to end against the
 * deterministic stack (smoke-test-images-ui.sh): an administrator picks the
 * organization image models and marks the chat model as accepting images in
 * Admin UI; a member asks for an image and an edit, attaches a picture the
 * model must receive inline, edits that upload, and every image comes back
 * through the owner-only content route, also after a reload. Organization
 * capabilities, model grants and image settings are restored.
 */
const ENABLED = process.env.E2E_IMAGES_UI === "true";
const BUSINESS_URL = process.env.E2E_BUSINESS_UI_URL || "http://localhost:3000";
const ADMIN_UI_URL = process.env.E2E_ADMIN_UI_URL || "http://localhost:3100";
const BUSINESS_API = process.env.E2E_BUSINESS_BACKEND_URL || "http://localhost:8000";
const ADMIN_API = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const KEYCLOAK_URL = process.env.E2E_KEYCLOAK_URL || "http://localhost:8080";
const CHAT_MODEL = process.env.E2E_IMAGES_CHAT_MODEL || "gpt-4o-mini";
const IMAGE_MODEL = process.env.E2E_IMAGES_MODEL || "test-image";

type Grant = { principalType: string; principalId: string; permission: string };
type ImageModels = {
  generationModel: string | null;
  editModel: string | null;
  visionModels: string[];
};
// A valid 1x1 PNG.
const UPLOAD_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64"
);

async function loginAt(page: Page, appUrl: string, landing: RegExp, username: string): Promise<void> {
  await page.goto(`${appUrl}/login`);
  await page.getByRole("button", { name: /sign in with keycloak/i }).click();
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(username);
  await page.locator("#kc-login").click();
  await page.waitForURL(landing, { timeout: 30_000 });
}

async function adminToken(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${KEYCLOAK_URL}/realms/aurum-ai/protocol/openid-connect/token`, {
    form: { grant_type: "password", client_id: "admin-ui", username: "admin", password: "admin" },
  });
  expect(response.ok()).toBeTruthy();
  return ((await response.json()) as { access_token: string }).access_token;
}

async function adminApi<T>(
  request: APIRequestContext,
  token: string,
  method: "GET" | "PUT",
  path: string,
  data?: unknown
): Promise<T> {
  const response = await request.fetch(`${ADMIN_API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  expect(response.status(), `${method} ${path}`).toBe(200);
  return ((await response.json()) as { data: T }).data;
}

async function adminCall<T>(
  request: APIRequestContext,
  token: string,
  method: "GET" | "POST" | "PUT",
  path: string,
  data: unknown,
  status: number
): Promise<T> {
  const response = await request.fetch(`${ADMIN_API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}` },
    data,
  });
  expect(response.status(), `${method} ${path}`).toBe(status);
  return ((await response.json()) as { data: T }).data;
}

// One reused agent and workflow, so repeated runs do not pile up definitions.
// Members see them only while this test grants them to the organization.
const AGENT_NAME = "Images e2e artist";
const WORKFLOW_NAME = "Images e2e workflow";

async function imageWorkflow(
  request: APIRequestContext,
  token: string
): Promise<{ agentId: string; workflowId: string }> {
  const agents = await adminApi<{ id: string; name: string }[]>(request, token, "GET", "/v1/agent-definitions");
  const agentId =
    agents.find((agent) => agent.name === AGENT_NAME)?.id ??
    (
      await adminCall<{ id: string }>(request, token, "POST", "/v1/agent-definitions", {
        name: AGENT_NAME,
        modelId: CHAT_MODEL,
        systemPrompt: "Create and refine pictures.",
        temperature: 0,
      }, 201)
    ).id;
  const draft = {
    name: WORKFLOW_NAME,
    description: "Image acceptance: one node draws, the next edits its picture.",
    supervisorAgentDefinitionId: agentId,
    autoRoutable: false,
    maxParallelism: 1,
    nodes: ["draw", "paint"].map((nodeKey) => ({
      nodeKey,
      displayName: nodeKey,
      agentDefinitionId: agentId,
      instruction: `MULTI_AGENT_IMAGE:${nodeKey}`,
      requiresConfirmation: false,
    })),
    edges: [{ sourceNodeKey: "draw", targetNodeKey: "paint" }],
  };
  const workflows = await adminApi<{ id: string; name: string }[]>(request, token, "GET", "/v1/admin/workflows");
  const existing = workflows.find((workflow) => workflow.name === WORKFLOW_NAME);
  if (!existing) {
    const created = await adminCall<{ id: string }>(request, token, "POST", "/v1/admin/workflows", draft, 201);
    return { agentId, workflowId: created.id };
  }
  const current = await adminApi<{ version: number }>(request, token, "GET", `/v1/admin/workflows/${existing.id}`);
  await adminApi(request, token, "PUT", `/v1/admin/workflows/${existing.id}`, { ...draft, version: current.version });
  return { agentId, workflowId: existing.id };
}

/** A browser-session API call, so it carries the member's own token. */
async function userApi(page: Page, path: string): Promise<{ status: number; body: unknown }> {
  return page.evaluate(async (url) => {
    const keycloak = (window as Window & { keycloak?: { token?: string } }).keycloak;
    const response = await fetch(url, { headers: { Authorization: `Bearer ${keycloak?.token}` } });
    const type = response.headers.get("content-type") ?? "";
    return {
      status: response.status,
      body: type.includes("json") ? await response.json() : type,
    };
  }, `${BUSINESS_API}${path}`);
}

async function expectLoadedImages(page: Page, count: number, kind = "Generated"): Promise<void> {
  const images = page.getByAltText(new RegExp(`^${kind} image \\d+$`));
  await expect(images).toHaveCount(count, { timeout: 30_000 });
  for (let index = 0; index < count; index += 1) {
    await expect
      .poll(() => images.nth(index).evaluate((node) => (node as HTMLImageElement).naturalWidth))
      .toBeGreaterThan(0);
  }
}

// Playwright traces persist request headers, including Keycloak bearer tokens.
test.use({ trace: "off" });

test.describe("Images: admin settings to chat generation, editing and attachments", () => {
  test.skip(!ENABLED, "E2E_IMAGES_UI is not enabled");

  test("generate, edit, attach and reload images with separately chosen models", async ({ browser, request }) => {
    test.setTimeout(420_000);
    const token = await adminToken(request);
    const subjects = await adminApi<{ principalType: string; principalId: string }[]>(
      request, token, "GET", "/v1/admin/authorization/subjects"
    );
    const organization = subjects.find((subject) => subject.principalType === "organization");
    expect(organization).toBeDefined();
    const originalCapabilities = (
      await adminApi<{ capabilities: string[] }>(request, token, "GET", "/v1/admin/authorization/capabilities/organization")
    ).capabilities;
    const originalGrants = (
      await adminApi<Grant[]>(
        request, token, "GET",
        `/v1/admin/authorization/grants?resourceType=model&resourceId=${encodeURIComponent(CHAT_MODEL)}`
      )
    ).map(({ principalType, principalId, permission }) => ({ principalType, principalId, permission }));
    const originalImages = await adminApi<ImageModels>(request, token, "GET", "/v1/admin/settings/images");
    const contexts: BrowserContext[] = [];
    const { agentId, workflowId } = await imageWorkflow(request, token);
    const workflowGrants: { resourceType: string; resourceId: string; grants: Grant[] }[] = [];
    for (const [resourceType, resourceId] of [["workflow", workflowId], ["agent_definition", agentId]]) {
      const grants = await adminApi<Grant[]>(
        request, token, "GET",
        `/v1/admin/authorization/grants?resourceType=${resourceType}&resourceId=${resourceId}`
      );
      workflowGrants.push({
        resourceType,
        resourceId,
        grants: grants.map(({ principalType, principalId, permission }) => ({ principalType, principalId, permission })),
      });
    }

    try {
      // Start from no image models so the Admin UI choice is a real change.
      await adminApi(request, token, "PUT", "/v1/admin/settings/images", {
        generationModel: null,
        editModel: null,
        visionModels: [],
      });
      await adminApi(request, token, "PUT", "/v1/admin/authorization/capabilities/organization", {
        capabilities: [...new Set([...originalCapabilities, "images.generate"])],
      });
      await adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
        resourceType: "model",
        resourceId: CHAT_MODEL,
        grants: [
          ...originalGrants.filter((grant) => grant.principalType !== "organization"),
          { principalType: "organization", principalId: organization!.principalId, permission: "read" },
        ],
      });
      for (const original of workflowGrants) {
        await adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
          resourceType: original.resourceType,
          resourceId: original.resourceId,
          grants: [
            ...original.grants.filter((grant) => grant.principalType !== "organization"),
            { principalType: "organization", principalId: organization!.principalId, permission: "read" },
          ],
        });
      }

      const adminContext = await browser.newContext();
      contexts.push(adminContext);
      const adminPage = await adminContext.newPage();
      await loginAt(adminPage, ADMIN_UI_URL, /\/admin\/users$/, "admin");
      await adminPage.goto(`${ADMIN_UI_URL}/admin/models`);
      const images = adminPage.getByRole("heading", { name: "Images" }).locator("xpath=ancestor::section[1]");
      await images.getByLabel("Image generation model").selectOption(IMAGE_MODEL);
      await images.getByLabel("Image editing model").selectOption(IMAGE_MODEL);
      await images.getByRole("checkbox", { name: CHAT_MODEL, exact: true }).check();
      const saved = adminPage.waitForResponse(
        (response) =>
          response.url().endsWith("/v1/admin/settings/images") &&
          response.request().method() === "PUT" &&
          response.status() === 200
      );
      await images.getByRole("button", { name: "Save image settings" }).click();
      await saved;

      const memberContext = await browser.newContext();
      contexts.push(memberContext);
      const member = await memberContext.newPage();
      await loginAt(member, BUSINESS_URL, /\/chat$/, "user");
      await member.getByRole("button", { name: "Choose model" }).click();
      await member.getByRole("option", { name: new RegExp(`^${CHAT_MODEL}`) }).click();
      await member.getByLabel("Message").fill("IMAGE_E2E_EDIT");
      await member.getByLabel("Send").click();

      await expect(member.getByTestId("assistant-content").last()).toContainText(
        "IMAGE_E2E_DONE edited",
        { timeout: 120_000 }
      );
      await expectLoadedImages(member, 2);

      const conversations = await userApi(member, "/v1/conversations?limit=1");
      expect(conversations.status).toBe(200);
      const conversationId = (conversations.body as { data: { id: string }[] }).data[0].id;
      const history = await userApi(member, `/v1/conversations/${conversationId}`);
      const messages = (history.body as { data: { messages: { data: { role: string; imageIds?: string[] }[] } } })
        .data.messages.data;
      const imageIds = messages.filter((message) => message.role === "assistant").flatMap((m) => m.imageIds ?? []);
      expect(imageIds).toHaveLength(2);
      const content = await userApi(member, `/v1/conversations/${conversationId}/images/${imageIds[0]}/content`);
      expect(content).toEqual({ status: 200, body: "image/png" });

      // A later turn edits an image of an earlier one: its id reaches the
      // model only through the conversation image list.
      await member.getByLabel("Message").fill("IMAGE_E2E_EDIT_PREVIOUS");
      await member.getByLabel("Send").click();
      await expect(member.getByTestId("assistant-content").last()).toContainText(
        "IMAGE_E2E_DONE edited",
        { timeout: 120_000 }
      );
      await expectLoadedImages(member, 3);

      // An attached picture reaches the vision model inline, and later turns
      // still send it from the conversation history.
      // The file input bypasses the button, so wait until a user could attach:
      // right after a turn the composer still treats it as active.
      await expect(member.getByRole("button", { name: "Attach file" })).toBeEnabled({ timeout: 30_000 });
      await member.getByLabel("Choose a file to attach").setInputFiles({
        name: "pixel.png",
        mimeType: "image/png",
        buffer: UPLOAD_PNG,
      });
      await expect(member.getByRole("img", { name: "pixel.png" })).toBeVisible({ timeout: 30_000 });
      await member.getByLabel("Message").fill("IMAGE_E2E_VISION what is this");
      await member.getByLabel("Send").click();
      await expect(member.getByTestId("assistant-content").last()).toContainText(
        "IMAGE_E2E_SEEN 1 of 1",
        { timeout: 120_000 }
      );
      await expectLoadedImages(member, 1, "Attached");
      await member.getByLabel("Message").fill("IMAGE_E2E_VISION again");
      await member.getByLabel("Send").click();
      await expect(member.getByTestId("assistant-content").last()).toContainText(
        "IMAGE_E2E_SEEN 0 of 1",
        { timeout: 120_000 }
      );

      // The upload is now the newest conversation image, so a follow-up edit
      // uses it as the source.
      await member.getByLabel("Message").fill("IMAGE_E2E_EDIT_PREVIOUS");
      await member.getByLabel("Send").click();
      await expect(member.getByTestId("assistant-content").last()).toContainText(
        "IMAGE_E2E_DONE edited",
        { timeout: 120_000 }
      );
      await expectLoadedImages(member, 4);
      // Later turns hydrate the conversation from history; the attachment stays.
      await expectLoadedImages(member, 1, "Attached");

      // A workflow: its first node draws, the next edits that picture, every
      // agent receives the attachment, and both images join the final answer.
      await member.reload();
      await member.getByRole("button", { name: new RegExp("IMAGE_E2E_EDIT") }).first().click();
      await expectLoadedImages(member, 4);
      await member.getByRole("button", { name: "Choose workflow" }).click();
      await member.getByRole("option", { name: new RegExp(`^${WORKFLOW_NAME}`) }).click();
      // The file input bypasses the button, so wait until a user could attach:
      // right after a turn the composer still treats it as active.
      await expect(member.getByRole("button", { name: "Attach file" })).toBeEnabled({ timeout: 30_000 });
      await member.getByLabel("Choose a file to attach").setInputFiles({
        name: "workflow.png",
        mimeType: "image/png",
        buffer: UPLOAD_PNG,
      });
      await expect(member.getByRole("img", { name: "workflow.png" })).toBeVisible({ timeout: 30_000 });
      await member.getByLabel("Message").fill("Draw a fox, then paint it");
      await member.getByLabel("Send").click();
      await expect(member.getByTestId("assistant-content").last()).toContainText(
        "MULTI_AGENT_IMAGE_FINAL:draw,paint SEEN 1",
        { timeout: 180_000 }
      );
      await expectLoadedImages(member, 6);
      await expectLoadedImages(member, 2, "Attached");

      // History rendering: every image after a full reload.
      await member.reload();
      await member.getByRole("button", { name: new RegExp("IMAGE_E2E_EDIT") }).first().click();
      await expectLoadedImages(member, 6);
      await expectLoadedImages(member, 2, "Attached");

      // Every image call is a counted request with the gateway's fixed usage.
      const today = new Date().toISOString().slice(0, 10);
      const usage = await adminApi<{ byModel: { model: string; requests: number; totalTokens: number; images: number }[] }>(
        request, token, "GET",
        `/v1/admin/analytics/usage?from=${today}&to=${today}&model=${encodeURIComponent(IMAGE_MODEL)}`
      );
      const imageRow = usage.byModel.find((row) => row.model === IMAGE_MODEL);
      expect(imageRow?.images).toBeGreaterThanOrEqual(6);
      expect(imageRow?.requests).toBe(imageRow?.images);
      expect(imageRow?.totalTokens).toBe(110 * (imageRow?.images ?? 0));
    } finally {
      await Promise.allSettled(contexts.map((context) => context.close()));
      // Restore every setting even if one restore fails, and never let a
      // restore failure hide the primary test failure.
      const restored = await Promise.allSettled([
        adminApi(request, token, "PUT", "/v1/admin/settings/images", {
          generationModel: originalImages.generationModel,
          editModel: originalImages.editModel,
          visionModels: originalImages.visionModels,
        }),
        adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", {
          resourceType: "model",
          resourceId: CHAT_MODEL,
          grants: originalGrants,
        }),
        adminApi(request, token, "PUT", "/v1/admin/authorization/capabilities/organization", {
          capabilities: originalCapabilities,
        }),
        ...workflowGrants.map((original) =>
          adminApi(request, token, "PUT", "/v1/admin/authorization/grants/reconcile", original)
        ),
      ]);
      const failures = restored.filter((result) => result.status === "rejected");
      if (failures.length) console.error(`image e2e restore failed for ${failures.length} setting(s)`);
    }
  });
});
