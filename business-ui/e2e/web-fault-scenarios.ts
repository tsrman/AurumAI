import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import { nextAssistantContent } from "./new-assistant";

type Proof = { status: string; reads: number; searches: number;
  operations: { kind: string; attempts: number; status: string; error: string | null }[] };
type History = { messages: { data: { id: string; role: string; turnId?: string }[] } };

export async function verifyWebFaults({ page, conversation, api, fixture, turn, phase }: {
  page: Page;
  conversation: string;
  api: <T>(endpoint: string) => Promise<T>;
  fixture: (command: string, id?: string, action?: string) => string;
  turn: (prompt: string, expected: string, phase?: string) => Promise<{ turnId: string }>;
  phase: (value: string) => void;
}) {
  const fault = (id: string, action: string) => JSON.parse(fixture("fault", id, action)) as {
    attempts: number; finished: number; released: boolean;
  };
  const proof = (id: string) => JSON.parse(fixture("turn-proof", id)) as Proof;
  for (const [mode, attempts, marker] of [
    ["429", 1, "WEB_E2E_ERROR:web_unavailable"],
    ["404", 1, "WEB_E2E_ERROR:web_unavailable"],
    ["503", 2, "WEB_E2E_FINAL"],
    ["deadline", 2, "WEB_E2E_ERROR:web_timeout"],
  ] as const) {
    phase(`fault-${mode}`);
    const id = randomUUID();
    fault(id, `arm/${mode}`);
    const started = performance.now();
    const final = await turn(`WEB_E2E_FAULT:${id}`, marker);
    const elapsed = performance.now() - started;
    expect(fault(id, "status").attempts, "exact upstream attempts").toBe(attempts);
    const row = proof(final.turnId!);
    expect(row.reads, "one logical read debit").toBe(1);
    expect(row.searches).toBe(0);
    expect(row.operations).toHaveLength(1);
    expect(row.operations[0].attempts).toBe(attempts);
    expect(row.operations[0].status).toBe(mode === "503" ? "completed" : "failed");
    if (mode !== "503") {
      expect(await api<unknown[]>(`/v1/conversations/${conversation}/turns/${final.turnId}/sources`)).toHaveLength(0);
    }
    if (mode === "deadline") {
      expect(elapsed, "real shared deadline does not reset on retry").toBeLessThan(38_000);
      expect(elapsed).toBeGreaterThan(25_000);
      expect(row.operations[0].error).toBe("web_timeout");
      // Observe the eventual upstream completion too; it cannot resurrect data.
      await expect.poll(() => fault(id, "status").finished, { timeout: 25_000 }).toBe(2);
      expect(proof(final.turnId!).operations[0].status).toBe("failed");
    }
  }

  for (const action of ["cancel", "revoke"] as const) {
    phase(`fault-${action}`);
    const id = randomUUID();
    fault(id, "arm/barrier");
    const before = await api<History>(`/v1/conversations/${conversation}`);
    const assistant = await nextAssistantContent(page,
      before.messages.data.filter(message => message.role === "assistant").map(message => message.id));
    phase(`fault-${action}-send`);
    await page.getByLabel("Message").fill(`WEB_E2E_FAULT:${id}`);
    await page.getByLabel("Send").click();
    await expect.poll(() => fault(id, "status").attempts, { timeout: 60_000 }).toBe(1);
    const active = await api<{ turnId: string }>(`/v1/conversations/${conversation}/turns/active`);
    expect(Boolean(active?.turnId)).toBe(true);
    try {
      phase(`fault-${action}-settle`);
      if (action === "cancel") {
        await page.getByLabel("Stop generating").click();
        // The upstream is STILL held. A lock spanning I/O makes this fail.
        await expect.poll(() => proof(active.turnId).status,
          { timeout: 10_000, message: "cancel settles before upstream release" }).toBe("cancelled");
        expect(fault(id, "status").released).toBe(false);
      } else {
        fixture("revoke");
      }
    } finally {
      fault(id, "release");
    }
    await expect.poll(() => fault(id, "status").finished, { timeout: 15_000 }).toBe(1);
    phase(`fault-${action}-late-data`);
    if (action === "revoke") {
      try {
        await expect(assistant).toHaveCount(1, { timeout: 60_000 });
        await expect(assistant)
          .toContainText("WEB_E2E_ERROR:web_access_denied", { timeout: 60_000 });
      } finally {
        fixture("grant");
      }
    }
    expect(await api<unknown[]>(`/v1/conversations/${conversation}/turns/${active.turnId}/sources`)).toHaveLength(0);
    if (action === "cancel") {
      const history = await api<History>(`/v1/conversations/${conversation}`);
      expect(history.messages.data.filter(m => m.role === "assistant" && m.turnId === active.turnId)).toHaveLength(0);
    }
    await expect(page.getByLabel("Message")).toBeEnabled({ timeout: 60_000 });
    phase(`fault-${action}-next-turn`);
    await turn("WEB_E2E_SEARCH", "WEB_E2E_FINAL", `fault-${action}-next-turn`);
    expect(await api<unknown[]>(`/v1/conversations/${conversation}/turns/${active.turnId}/sources`)).toHaveLength(0);
  }
}
