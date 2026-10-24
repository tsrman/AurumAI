import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";
import { expectSelectedModel } from "./helpers/chat";
import { multistepMathSkillBundle } from "./helpers/skillBundle";

/**
 * Full real round trip for a *multi-round* code skill call, driven through
 * the actual browser: upload a real bundle -> real LLM emits <tool_call> ->
 * CodeSkillSessionWorkflow opens a sandbox session -> the model runs a first
 * command, sees its real stdout, runs a second command that depends on it ->
 * session closes -> result rendered in the UI. The "multistep" skill can
 * only pass with genuine multi-round sandbox execution (see
 * conversation-runner/src/workflows/code_skill_session.py) — a single
 * fire-and-forget command isn't enough.
 *
 * Same opt-in gate as chat.real-llm.spec.ts (E2E_USE_REAL_LLM), plus:
 *   docker compose --profile skills-exec up -d skill-executor
 *   docker compose --profile build build skill-sandbox
 *
 * The bundle upload is an out-of-band API call, not a UI drive. It used to
 * fill business-ui's own /admin/agents dialog, but that page no longer
 * exists here: skills admin moved to admin-ui on :3100 with its own Keycloak
 * client (docs/ARCHITECTURE.md), so the goto() silently
 * landed on a 404 and the test hung until timeout waiting for a dialog that
 * could never appear. The upload UI itself is covered where it now lives, by
 * admin-ui/e2e/admin-skills-code.spec.ts; what only this spec can cover is
 * the chat half — a code skill actually running multiple sandbox rounds and
 * rendering in business-ui — so that is all it drives through the browser
 * now. POST/DELETE of skills live on the admin plane (:8001) and need an
 * admin-ui-audience token, fetched straight from Keycloak the same way
 * the admin-plane smoke does; a business-ui token gets a plain 401 there.
 *
 * The chat half runs as admin purely so one login covers everything — the
 * uploaded skill is exposed through explicit v2 provisioning grants.
 */
const ENABLED = process.env.E2E_USE_REAL_LLM === "true";
const MODEL = process.env.E2E_LLM_MODEL || "";
if (ENABLED && !MODEL) {
  throw new Error("Set E2E_LLM_MODEL when E2E_USE_REAL_LLM=true.");
}
const ADMIN_BACKEND_URL = process.env.E2E_ADMIN_BACKEND_URL || "http://localhost:8001";
const KEYCLOAK_URL = process.env.E2E_KEYCLOAK_URL || "http://localhost:8080";
const KEYCLOAK_REALM = process.env.E2E_KEYCLOAK_REALM || "aurum-ai";

async function getAdminUiToken(): Promise<string> {
  const res = await fetch(
    `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token`,
    {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "password",
        client_id: "admin-ui",
        username: "admin",
        password: "admin",
      }),
    }
  );
  const json = (await res.json()) as { access_token?: string };
  if (!json.access_token) throw new Error("Could not get an admin-ui token from Keycloak");
  return json.access_token;
}

/** Real multipart upload against POST /v1/skills/code — same request the
 *  admin-ui dialog sends, so the bundle still goes through the genuine
 *  validation/MinIO path (skill_bundle.py), just without the UI in front. */
async function uploadCodeSkill(token: string, name: string, bundle: Buffer): Promise<string> {
  const form = new FormData();
  form.append("name", name);
  form.append("description", "e2e multistep code skill");
  // new Uint8Array(...) rather than the Buffer directly: Buffer is typed as
  // Uint8Array<ArrayBufferLike>, which TS won't accept as a BlobPart.
  form.append(
    "bundle",
    new Blob([new Uint8Array(bundle)], { type: "application/zip" }),
    "bundle.zip"
  );
  const res = await fetch(`${ADMIN_BACKEND_URL}/v1/skills/code`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  if (!res.ok) {
    throw new Error(`Code skill upload failed: ${res.status} ${await res.text()}`);
  }
  const json = (await res.json()) as { data: { id: string } };
  return json.data.id;
}

test.describe("Chat: real multi-round code skill call", () => {
  test.skip(!ENABLED, "E2E_USE_REAL_LLM is not set; test skipped");

  // Computed inside beforeAll (worker-process time), not at module scope —
  // a value baked into the test title itself would differ between
  // Playwright's list phase and run phase (separate module evaluations) and
  // the worker would fail with "Test not found in the worker process".
  let skillName = "";
  test.beforeAll(() => {
    skillName = `e2e-realskill-multistep-${Date.now()}`;
  });

  // Set once the skill is created — afterEach (unlike cleanup at the end of
  // the happy path) also runs when an assertion fails partway through, so a
  // failed run doesn't leak the uploaded skill/bundle.
  let skillId = "";
  let adminUiToken = "";
  test.afterEach(async () => {
    if (!skillId || !adminUiToken) return;
    await fetch(`${ADMIN_BACKEND_URL}/v1/skills/${skillId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${adminUiToken}` },
    }).catch(() => {});
  });

  test(`multi-round sandbox session through model ${MODEL}`, async ({ page }) => {
    // Default Playwright per-test timeout (30s) isn't enough for a real
    // multi-round session: bundle upload + 2+ LLM round trips + 2 sandbox
    // execs + a final synthesis call.
    test.setTimeout(180_000);

    // The LLM's <tool_call> carries the skill's id, not its display name
    // (see conversation.py's _build_skills_system_message — "skill_id":
    // "<id>") — that's what ends up in the ToolCallRow's aria-label, so the
    // real id comes from the upload response rather than being assumed.
    adminUiToken = await getAdminUiToken();
    skillId = await uploadCodeSkill(adminUiToken, skillName, multistepMathSkillBundle(skillName));

    // Log in only after the skill exists: the available-skills list is built
    // when the chat turn starts, so ordering doesn't strictly matter, but
    // this keeps the browser session as short as the chat half needs.
    await login(page, "admin", "admin");
    await page.getByRole("button", { name: "Choose model" }).click();
    await page.getByRole("option", { name: MODEL, exact: true }).click();

    // Explicit "(skill_id: <uuid>)" — without it, the model sometimes uses
    // the human-readable name as the skill_id in its <tool_call> (it's what
    // it was just told to "call"), which the backend can't resolve. Same
    // phrasing smoke-test-code-skill.sh uses for the same reason.
    const composer = page.getByLabel("Message");
    await composer.fill(
      `Call the ${skillName} skill (skill_id: ${skillId}) with args {"task": "compute the two-step result"} and tell me the FINAL RESULT number.`
    );
    await expectSelectedModel(page, MODEL);
    await page.getByLabel("Send").click();

    // aria-label lives on a plain <div> (see ToolCallRow.tsx) — getByLabel()
    // targets labelled form controls, so an attribute locator is used here.
    await expect(page.locator(`[aria-label="${skillId}: Done"]`).first()).toBeVisible({
      timeout: 120_000,
    });
    await expect(page.getByLabel("Send")).toBeVisible({ timeout: 120_000 });
    // Scoped to the final answer's own testid, not exact-matched — "142"
    // also appears as a substring of the tool-call detail line rendered by
    // ToolCallRow (also a <p>, so a bare page-wide getByText("142") would
    // hit strict-mode violations), and not every model phrases the answer
    // as the bare number (e.g. "The FINAL RESULT number is 142.").
    await expect(
      page.getByTestId("assistant-content").last()
    ).toContainText(/\b142\b/);
  });
});
