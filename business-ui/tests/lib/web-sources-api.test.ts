import { beforeEach, expect, test, vi } from "vitest";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock("@/lib/api", () => ({ api: { get } }));

import {
  getWebSources,
  loadSourcesForMessage,
} from "@/lib/web-sources-api";

beforeEach(() => {
  get.mockReset();
});

test("loads web sources through the exact conversation and turn route", async () => {
  const sources = [
    {
      sourceId: "source-1",
      kind: "read" as const,
      requestedUrl: "https://example.org/report",
      finalUrl: "https://example.org/report",
      title: "Report",
      contentType: "text/html",
      snippet: "Public report",
      publishedAt: null,
      retrievedAt: "2026-09-07T00:00:00Z",
      truncated: false,
      pageCount: null,
    },
  ];
  get.mockReturnValue({
    json: vi.fn().mockResolvedValue({ data: sources, meta: {} }),
  });

  await expect(getWebSources("conversation-1", "turn-1")).resolves.toEqual(
    sources
  );
  expect(get).toHaveBeenCalledWith(
    "conversations/conversation-1/turns/turn-1/sources"
  );
});

test("fails closed when verified source metadata cannot be loaded", async () => {
  get.mockReturnValue({
    json: vi.fn().mockRejectedValue(new Error("offline")),
  });

  await expect(
    loadSourcesForMessage("conversation-1", {
      id: "message-1",
      role: "assistant",
      content: "Answer",
      model: "test",
      tokensIn: 0,
      tokensOut: 0,
      costUsd: 0,
      latencyMs: null,
      toolCalls: null,
      toolResults: null,
      createdAt: "2026-09-07T00:00:00Z",
      turnId: "turn-1",
      sourceIds: ["source-1"],
      unsupportedSourceIds: ["unknown-source"],
    })
  ).resolves.toEqual([
    { kind: "unsupported", index: 1, sourceId: "unknown-source" },
  ]);
});
