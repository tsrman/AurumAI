import { test as base } from "@playwright/test";

// Installed Playwright's supported gate: no automatic aria/page snapshots.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1";

const PHASES = [
  "setup",
  "login",
  "sources-create",
  "sources-turn",
  "sources-turn-send",
  "sources-turn-render",
  "sources-turn-stream",
  "sources-turn-stream-message-end",
  "sources-turn-stream-marker",
  "sources-turn-stream-empty-search",
  "sources-turn-stream-invalid-tool-data",
  "sources-turn-stream-web-unavailable",
  "sources-turn-stream-web-timeout",
  "sources-turn-stream-fixture-not-selected",
  "sources-turn-history",
  "sources-api",
  "sources-owner",
  "sources-ui",
  "history",
  "history-reload",
  "history-reload-auth",
  "history-navigation",
  "history-navigation-auth",
  "history-open",
  "history-sources",
  "reconnect",
  "upstream-faults",
  "mixed-question-web",
  "mixed-question-web-stream",
  "mixed-question-web-render",
  "mixed-question-web-history",
  "fault-429",
  "fault-404",
  "fault-503",
  "fault-deadline",
  "fault-cancel",
  "fault-cancel-send",
  "fault-cancel-settle",
  "fault-cancel-late-data",
  "fault-cancel-next-turn",
  "fault-cancel-next-turn-stream",
  "fault-cancel-next-turn-render-count",
  "fault-cancel-next-turn-render-marker",
  "fault-cancel-next-turn-history",
  "fault-revoke",
  "fault-revoke-send",
  "fault-revoke-settle",
  "fault-revoke-late-data",
  "fault-revoke-next-turn",
  "fault-revoke-next-turn-stream",
  "fault-revoke-next-turn-render-count",
  "fault-revoke-next-turn-render-marker",
  "fault-revoke-next-turn-history",
  "multi-agent-prepare",
  "multi-agent-question-wait",
  "multi-agent-interactive-final",
  "multi-agent-question-budget",
  "multi-agent-question-budget-answer",
  "multi-agent-question-budget-final",
  "multi-agent-budget",
  "multi-agent-revoke-held",
  "errors",
  "errors-hostile",
  "errors-pdf",
  "errors-pdf-web-unavailable",
  "errors-pdf-web-timeout",
  "errors-pdf-final",
  "errors-pdf-marker",
  "errors-html",
  "errors-binary",
  "errors-large",
  "errors-redirect",
  "limits",
  "revoke",
  "ownership",
  "cleanup",
] as const;

export const test = base.extend<{ webPhase: (phase: unknown) => void }>({
  webPhase: [async ({}, use, info) => {
    let phase: string = "setup";
    await use((candidate) => {
      // Never preserve raw step titles, error text, URLs or caller values.
      const next = PHASES.find((allowed) => allowed === candidate) ?? "unknown";
      if (next !== phase) process.stdout.write(`[web-phase:${next}]\n`);
      phase = next;
    });
    // Locator errors can contain text, URLs and form values even with traces
    // disabled. Sanitize before reporters/error-context generation run.
    for (const error of info.errors) {
      // Playwright's matcher error may carry an internal errorContext/ARIA
      // snapshot even when the automatic snapshot gate is enabled.
      for (const key of Object.keys(error)) Reflect.deleteProperty(error, key);
      error.message = `[web-phase:${phase}] Private web acceptance assertion failed; content diagnostics suppressed.`;
      error.stack = error.message;
    }
  }, { auto: true }],
});
test.use({ trace: "off", video: "off", screenshot: "off", storageState: undefined });
