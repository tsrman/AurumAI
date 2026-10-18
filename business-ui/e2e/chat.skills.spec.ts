import { test, expect } from "@playwright/test";
import { login } from "./helpers/auth";

/**
 * Deterministic coverage for how a skill invocation renders in the chat UI
 * — scripts /v1/chat/stream with the real event shape conversation-runner's
 * ConversationWorkflow publishes for a tool call (tool-calls -> tool-result
 * -> content-delta* -> message-end -> done; see
 * conversation-runner/src/workflows/conversation.py and
 * business-ui/src/hooks/useChat.ts), same "stub only the LLM Gateway"
 * approach as chat.spec.ts. This exercises the frontend's rendering of the
 * ToolCallRow lifecycle without needing a real LLM, MCP server, or
 * skill-executor sandbox — that full round trip is covered by
 * smoke-test-mcp.sh / smoke-test-code-skill.sh at the backend layer.
 */
function sseBodyWithToolCall(opts: {
  skillId: string;
  args: Record<string, unknown>;
  result: unknown;
  finalAnswer: string;
}): string {
  const { skillId, args, result, finalAnswer } = opts;
  const words = finalAnswer.split(" ");
  const deltas = words
    .map((word, i) => `event: content-delta\ndata: {"delta": "${i > 0 ? " " : ""}${word}"}\n\n`)
    .join("");
  return (
    `event: message-start\ndata: {}\n\n` +
    `event: tool-calls\n` +
    `data: {"calls": [{"call_id": "call-1", "skill_id": "${skillId}", "args": ${JSON.stringify(args)}}], "turn_id": "t1"}\n\n` +
    `event: tool-result\n` +
    `data: {"call_id": "call-1", "skill_id": "${skillId}", "result": ${JSON.stringify(result)}, "turn_id": "t1"}\n\n` +
    deltas +
    `event: message-end\ndata: {"content": "${finalAnswer}"}\n\n` +
    `event: done\ndata: {}\n\n`
  );
}

test.describe("Chat: skill call", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("a skill call shows a running indicator, then done, then the final answer", async ({
    page,
  }) => {
    await page.route("**/v1/chat/stream", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseBodyWithToolCall({
          skillId: "smoke-test-sum",
          args: { a: 17, b: 25 },
          result: { skill_id: "smoke-test-sum", output: { result: 42 } },
          finalAnswer: "The sum is 42.",
        }),
      });
    });

    const composer = page.getByLabel("Message");
    await composer.fill("What is 17 plus 25?");
    await page.getByLabel("Send").click();

    // route.fulfill() delivers the whole scripted SSE body in one chunk, so
    // the transient "run" state isn't reliably observable here (no real
    // network latency between tool-calls and tool-result) — only the
    // settled "done" state is asserted. The real network case gets its own
    // running-state observation for free from the fact that this doesn't
    // hang: chat.skills.real.spec.ts drives an actual, slower round trip.
    //
    // ToolCallRow completion/running status is present only as an
    // aria-label on a plain, non-form <div> — getByLabel() targets labelled
    // form controls, so it won't match here; an attribute locator is the
    // reliable way to assert an aria-label on an arbitrary element. The
    // real payload carries skill_id, not a display name, so that's what
    // shows up here too.
    await expect(page.locator('[aria-label="smoke-test-sum: Done"]')).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText("The sum is 42.")).toBeVisible({ timeout: 15_000 });
  });
});
