import { beforeEach, expect, test, vi } from "vitest";

const { get, post, del } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: { get, post, delete: del } }));

import {
  attachFileToConversation,
  deleteDocument,
  listDocuments,
  uploadDocument,
} from "@/lib/documents-api";

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  del.mockReset();
});

test("scopes document list by knowledge base", async () => {
  get.mockReturnValue({ json: vi.fn().mockResolvedValue({ data: [], meta: {} }) });

  await listDocuments("kb-1");

  expect(get).toHaveBeenCalledWith("documents", {
    searchParams: { knowledge_base_id: "kb-1" },
  });
});

test("uploads the trusted knowledge base id in multipart data", async () => {
  const file = new File(["body"], "runbook.md", { type: "text/markdown" });
  post.mockImplementation((_path: string, options: { body: FormData }) => {
    expect(options.body.get("knowledge_base_id")).toBe("kb-1");
    expect(options.body.get("file")).toBe(file);
    return { json: vi.fn().mockResolvedValue({ data: { id: "doc-1" }, meta: {} }) };
  });

  await uploadDocument({ knowledgeBaseId: "kb-1", file });

  expect(post).toHaveBeenCalledWith(
    "documents/upload",
    expect.objectContaining({ timeout: 120_000 })
  );
});

test("attaches a file to a conversation, not a knowledge base", async () => {
  const file = new File(["body"], "notes.md", { type: "text/markdown" });
  post.mockImplementation((path: string, options: { body: FormData }) => {
    expect(path).toBe("conversations/conv-1/attachments");
    expect(options.body.get("file")).toBe(file);
    expect(options.body.get("knowledge_base_id")).toBeNull();
    return {
      json: vi
        .fn()
        .mockResolvedValue({ data: { id: "doc-1", knowledge_base_id: "kb-1" }, meta: {} }),
    };
  });

  const doc = await attachFileToConversation({ conversationId: "conv-1", file });

  expect(post).toHaveBeenCalledWith(
    "conversations/conv-1/attachments",
    expect.objectContaining({ timeout: 120_000 })
  );
  expect(doc.knowledge_base_id).toBe("kb-1");
});

test("scopes document deletion by knowledge base", async () => {
  del.mockResolvedValue(undefined);

  await deleteDocument({ id: "doc-1", knowledgeBaseId: "kb-1" });

  expect(del).toHaveBeenCalledWith("documents/doc-1", {
    searchParams: { knowledge_base_id: "kb-1" },
  });
});
