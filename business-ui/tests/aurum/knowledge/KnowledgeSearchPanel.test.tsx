import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import { KnowledgeSearchPanel } from "@/components/aurum/knowledge/KnowledgeSearchPanel";
import type { DocumentDto } from "@/lib/documents-api";
import type { KnowledgeSearchResponse } from "@/lib/knowledge-api";

const search = vi.fn();
let isPending = false;

vi.mock("@/hooks/useKnowledgeSearch", () => ({
  useKnowledgeSearch: () => ({ search, isPending }),
}));

const documents: DocumentDto[] = [
  {
    id: "doc-1",
    knowledge_base_id: "kb-1",
    title: "Runbook",
    source_filename: "runbook.txt",
    status: "indexed",
    chunk_count: 3,
    error: null,
    created_at: "2026-09-03T10:00:00Z",
    updated_at: "2026-09-03T10:01:00Z",
  },
];

beforeEach(() => {
  search.mockReset();
  isPending = false;
});

test("does not search for a blank trimmed query", () => {
  render(<KnowledgeSearchPanel knowledgeBaseId="kb-1" documents={documents} />);

  fireEvent.change(screen.getByLabelText("Search the knowledge base"), {
    target: { value: "   " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));

  expect(search).not.toHaveBeenCalled();
});

test("renders result content as text with its canonical source and score", async () => {
  const response: KnowledgeSearchResponse = {
    results: [
      {
        content: "<script>alert(1)</script>",
        relevance_score: 0.0156,
        metadata: { document_id: "doc-1", document_title: "fallback.txt" },
      },
    ],
    warnings: ["Search completed with limitations"],
  };
  search.mockResolvedValue(response);
  const { container } = render(
    <KnowledgeSearchPanel knowledgeBaseId="kb-1" documents={documents} />
  );

  fireEvent.change(screen.getByLabelText("Search the knowledge base"), {
    target: { value: " unique marker " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));

  await waitFor(() => expect(search).toHaveBeenCalledWith("unique marker"));
  expect(await screen.findByText("runbook.txt")).toBeInTheDocument();
  expect(screen.getByText("Relevance: 0.016")).toBeInTheDocument();
  expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent(
    "Search completed with limitations"
  );
});

test("distinguishes an empty result from a request failure", async () => {
  search.mockResolvedValueOnce({ results: [], warnings: [] });
  const { unmount } = render(
    <KnowledgeSearchPanel knowledgeBaseId="kb-1" documents={documents} />
  );

  fireEvent.change(screen.getByLabelText("Search the knowledge base"), {
    target: { value: "missing" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(await screen.findByText("Nothing found")).toBeInTheDocument();

  unmount();
  search.mockRejectedValueOnce(new TypeError("private network detail"));
  render(<KnowledgeSearchPanel knowledgeBaseId="kb-1" documents={documents} />);
  fireEvent.change(screen.getByLabelText("Search the knowledge base"), {
    target: { value: "broken" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Network error");
  expect(screen.queryByText("Nothing found")).not.toBeInTheDocument();
});

test("disables search while a request is pending", () => {
  isPending = true;
  render(<KnowledgeSearchPanel knowledgeBaseId="kb-1" documents={documents} />);

  expect(screen.getByRole("button", { name: "Searching…" })).toBeDisabled();
});

