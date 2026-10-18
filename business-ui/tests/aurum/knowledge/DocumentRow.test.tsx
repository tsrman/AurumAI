import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { DocumentRow } from "@/components/aurum/knowledge/DocumentRow";
import type { DocumentDto } from "@/lib/documents-api";

const documentDto: DocumentDto = {
  id: "doc-1",
  knowledge_base_id: "kb-1",
  title: "Fallback title",
  source_filename: "runbook.txt",
  status: "indexed",
  chunk_count: 7,
  error: null,
  created_at: "2026-09-03T10:00:00Z",
  updated_at: "2026-09-03T10:01:00Z",
};

function renderRow(
  overrides: Partial<React.ComponentProps<typeof DocumentRow>> = {}
) {
  const props: React.ComponentProps<typeof DocumentRow> = {
    doc: documentDto,
    canDelete: true,
    deleting: false,
    onDelete: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(
    <table>
      <tbody>
        <DocumentRow {...props} />
      </tbody>
    </table>
  );
  return props;
}

test("deletes the canonical document ID only after confirmation", async () => {
  const props = renderRow();

  fireEvent.click(
    screen.getByRole("button", { name: "Delete document runbook.txt" })
  );
  expect(props.onDelete).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  await waitFor(() => expect(props.onDelete).toHaveBeenCalledWith("doc-1"));
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});

test("does not expose deletion for a read-only base", () => {
  renderRow({ canDelete: false });

  expect(
    screen.queryByRole("button", { name: /Delete document/ })
  ).not.toBeInTheDocument();
});

test("keeps confirmation open and allows retry after a safe failure", async () => {
  const onDelete = vi.fn().mockRejectedValue(new TypeError("private network detail"));
  renderRow({ onDelete });

  fireEvent.click(
    screen.getByRole("button", { name: "Delete document runbook.txt" })
  );
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Network error");
  expect(screen.getByRole("alertdialog")).toBeInTheDocument();
});

test("shows a safe failed-indexing message without internal details", () => {
  renderRow({
    doc: {
      ...documentDto,
      status: "failed",
      error: "Traceback: embedding password=secret",
    },
  });

  expect(screen.getByText("The document could not be processed")).toBeInTheDocument();
  expect(screen.queryByText(/password=secret/)).not.toBeInTheDocument();
});

