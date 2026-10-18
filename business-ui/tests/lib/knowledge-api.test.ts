import { beforeEach, expect, test, vi } from "vitest";

const { get, post, del } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: { get, post, delete: del } }));

import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  listKnowledgeBases,
  searchKnowledgeBase,
} from "@/lib/knowledge-api";

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  del.mockReset();
});

test("lists canonical knowledge bases from the backend", async () => {
  const rows = [
    {
      id: "11111111-1111-1111-1111-111111111111",
      name: "Runbooks",
      description: "Operations",
      canWrite: true,
      createdAt: "2026-09-01T00:00:00Z",
      updatedAt: "2026-09-01T00:00:00Z",
    },
  ];
  get.mockReturnValue({
    json: vi.fn().mockResolvedValue({ data: rows, meta: {} }),
  });

  await expect(listKnowledgeBases()).resolves.toEqual(rows);
  expect(get).toHaveBeenCalledWith("knowledge-bases");
});

test("creates a persistent knowledge base", async () => {
  const row = {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Runbooks",
    description: null,
    canWrite: true,
    createdAt: "2026-09-01T00:00:00Z",
    updatedAt: "2026-09-01T00:00:00Z",
  };
  post.mockReturnValue({
    json: vi.fn().mockResolvedValue({ data: row, meta: {} }),
  });

  await expect(
    createKnowledgeBase({ name: "Runbooks", description: "" })
  ).resolves.toEqual(row);
  expect(post).toHaveBeenCalledWith("knowledge-bases", {
    json: { name: "Runbooks", description: "" },
  });
});

test("deletes the canonical knowledge base id", async () => {
  del.mockResolvedValue(undefined);

  await deleteKnowledgeBase("kb-1");

  expect(del).toHaveBeenCalledWith("knowledge-bases/kb-1");
});

test("searches one knowledge base with the requested limit", async () => {
  const response = {
    results: [
      {
        content: "Gold recovery is 92 percent.",
        relevance_score: 0.25,
        metadata: {
          document_id: "doc-1",
          document_title: "runbook",
        },
      },
    ],
    warnings: [],
  };
  post.mockReturnValue({ json: vi.fn().mockResolvedValue(response) });

  await expect(
    searchKnowledgeBase({
      knowledgeBaseId: "kb-1",
      query: "gold recovery",
      limit: 10,
    })
  ).resolves.toEqual(response);
  expect(post).toHaveBeenCalledWith("knowledge-bases/kb-1/search", {
    json: { query: "gold recovery", limit: 10 },
  });
});
