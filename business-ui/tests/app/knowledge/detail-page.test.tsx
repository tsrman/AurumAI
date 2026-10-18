import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import KnowledgeDetailPage from "@/app/knowledge/[id]/page";
import type { DocumentDto } from "@/lib/documents-api";
import type { KnowledgeBase } from "@/types/aurum";

const deleteDocument = vi.fn();
const deleteBase = vi.fn();
const uploadDocument = vi.fn();
const push = vi.fn();
let documents: DocumentDto[] = [];

const base: KnowledgeBase = {
  id: "kb-1",
  name: "Runbooks",
  badge: "RU",
  badgeColor: "amber",
  description: "Operations",
  canWrite: true,
  docCount: 1,
  size: "—",
  updated: "03.09.2026",
  status: "indexed",
  docs: [],
};

const documentDto: DocumentDto = {
  id: "doc-1",
  knowledge_base_id: "kb-1",
  title: "Runbook",
  source_filename: "runbook.txt",
  status: "indexed",
  chunk_count: 3,
  error: null,
  created_at: "2026-09-03T10:00:00Z",
  updated_at: "2026-09-03T10:01:00Z",
};

vi.mock("@/hooks/useKnowledge", () => ({
  useKnowledge: () => ({
    knowledgeBases: [base],
    isLoading: false,
    deleteBase,
    isDeleting: false,
  }),
}));

vi.mock("@/hooks/useDocuments", () => ({
  useDocuments: () => ({ data: documents, isLoading: false }),
  useUploadDocument: () => ({ mutateAsync: uploadDocument, isPending: false }),
  useDeleteDocument: () => ({ mutateAsync: deleteDocument, isPending: false }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/hooks/useKnowledgeSearch", () => ({
  useKnowledgeSearch: () => ({ search: vi.fn(), isPending: false }),
}));

beforeEach(() => {
  documents = [documentDto];
  deleteDocument.mockReset().mockResolvedValue(undefined);
  deleteBase.mockReset().mockResolvedValue(undefined);
  uploadDocument.mockReset().mockResolvedValue(undefined);
  push.mockReset();
});

test("keeps base deletion visible but disabled until documents are removed", async () => {
  await act(async () => {
    render(<KnowledgeDetailPage params={Promise.resolve({ id: "kb-1" })} />);
  });

  expect(screen.getByRole("button", { name: "Delete knowledge base" })).toBeDisabled();
  expect(screen.getByText("Delete the documents first")).toBeInTheDocument();
});

test("deletes an empty writable base and returns to the knowledge list", async () => {
  documents = [];
  await act(async () => {
    render(<KnowledgeDetailPage params={Promise.resolve({ id: "kb-1" })} />);
  });

  fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  await waitFor(() => expect(deleteBase).toHaveBeenCalledWith("kb-1"));
  expect(push).toHaveBeenCalledWith("/knowledge");
});

test("passes the canonical document ID from the row to the delete mutation", async () => {
  await act(async () => {
    render(<KnowledgeDetailPage params={Promise.resolve({ id: "kb-1" })} />);
  });

  const deleteButton = await screen.findByRole("button", {
    name: "Delete document runbook.txt",
  });
  fireEvent.click(deleteButton);
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  await waitFor(() => expect(deleteDocument).toHaveBeenCalledWith("doc-1"));
});

test("mounts read-only knowledge search with the loaded documents", async () => {
  await act(async () => {
    render(<KnowledgeDetailPage params={Promise.resolve({ id: "kb-1" })} />);
  });

  expect(screen.getByLabelText("Search the knowledge base")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Search" })).toBeInTheDocument();
});
