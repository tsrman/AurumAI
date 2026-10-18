import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { DocumentTable } from "@/components/aurum/knowledge/DocumentTable";
import type { DocumentDto } from "@/lib/documents-api";

const docs: DocumentDto[] = [
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

test("renders canonical DTO rows with a write-only action column", () => {
  render(
    <DocumentTable
      docs={docs}
      canWrite
      deletingId={null}
      onDelete={vi.fn()}
    />
  );

  expect(screen.getByRole("columnheader", { name: "Actions" })).toBeInTheDocument();
  expect(screen.getByText("runbook.txt")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Delete document runbook.txt" })).toBeEnabled();
});

test("omits the action column for a read-only base", () => {
  render(
    <DocumentTable
      docs={docs}
      canWrite={false}
      deletingId={null}
      onDelete={vi.fn()}
    />
  );

  expect(screen.queryByRole("columnheader", { name: "Actions" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Delete document/ })).not.toBeInTheDocument();
});

