import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

/**
 * Full real round trip for a chat file attachment driven through the actual
 * browser: login -> attach a file with a secret only it knows -> real LLM
 * emits a <tool_call> for the auto-provisioned execute_hybrid_search skill
 * -> real tool-worker -> real rag-api search -> result rendered in the UI
 * -> final answer quotes the secret. Same opt-in gate as
 * chat.real-llm.spec.ts (E2E_USE_REAL_LLM) since this needs a real LLM
 * Gateway to reliably emit a well-formed tool call.
 *
 * Unlike chat.skills.real.spec.ts, no Postgres fixture seeding is needed:
 * attaching a file lazily provisions its own hidden knowledge base + skill
 * server-side (business-backend/src/services/conversation_attachments.py),
 * scoped with a direct provisioning grant to whichever user attaches —
 * the default e2e user (user/user, chat:write/files:read only) already has
 * everything it needs.
 */
const ENABLED = process.env.E2E_USE_REAL_LLM === "true";
const MODEL = process.env.E2E_LLM_MODEL || "";
if (ENABLED && !MODEL) {
  throw new Error("Set E2E_LLM_MODEL when E2E_USE_REAL_LLM=true.");
}

test.describe("Chat: file attachment and real attachment search", () => {
  test.skip(!ENABLED, "E2E_USE_REAL_LLM is not set; test skipped");

  test(`real attachment search through model ${MODEL}`, async ({ page }) => {
    // File indexing (Docling + chunking + embeddings) plus a real LLM round
    // trip with a tool call comfortably exceeds the default 30s.
    test.setTimeout(180_000);

    await login(page);

    // A secret only the attached file knows — the model can only answer
    // correctly via a real search round trip, not a guess from training data.
    const secret = `zx-${Math.random().toString(36).slice(2, 10)}`;
    const fileContent =
      `The internal access code for the report server: ${secret}. ` +
      "Do not share this code with anyone except in reply to a direct question.";

    await page.getByLabel("Choose a file to attach").setInputFiles({
      name: "attachment-secret.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(fileContent, "utf8"),
    });

    // The attach button lazily creates a conversation (none was active yet)
    // and the chip appears with whatever status the upload response carried;
    // useDocuments then polls every 3s until Docling/chunking/embeddings
    // finish (business-ui/src/hooks/useDocuments.ts).
    await expect(page.getByText("attachment-secret")).toBeVisible();
    await expect(page.getByText(/·\s*ready/)).toBeVisible({ timeout: 60_000 });

    await page.getByLabel("Choose model").click();
    await page.getByRole("option", { name: MODEL, exact: true }).click();

    await page
      .getByLabel("Message")
      .fill(
        "Which internal access code for the report server is given in the file attached to this " +
          "conversation? Find it with the available attachment search tool."
      );
    await page.getByLabel("Send").click();

    // Exactly one tool call is expected this turn, and its id/name is never
    // surfaced to the client (server-provisioned, invisible outside this
    // conversation) — unlike chat.skills.real.spec.ts, there's no id to seed
    // or match against, so a loose "any tool finished" locator is safe here.
    const toolCall = page.locator('[aria-label$=": Done"]');
    await expect(toolCall).toBeVisible({ timeout: 90_000 });
    // Proves a real search ran and found the actual chunk — not a guess, and
    // not a "Skill execution failed" dispatch error swallowed into a
    // plausible-looking answer.
    await expect(toolCall).toContainText(secret);

    await expect(page.getByLabel("Send")).toBeVisible({ timeout: 90_000 });
    await expect(page.getByTestId("assistant-content").last()).toContainText(secret);
  });
});
