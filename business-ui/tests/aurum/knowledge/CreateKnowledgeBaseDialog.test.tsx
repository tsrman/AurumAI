import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { CreateKnowledgeBaseDialog } from "@/components/aurum/knowledge/CreateKnowledgeBaseDialog";

function renderDialog(
  overrides: Partial<React.ComponentProps<typeof CreateKnowledgeBaseDialog>> = {}
) {
  const props: React.ComponentProps<typeof CreateKnowledgeBaseDialog> = {
    open: true,
    pending: false,
    error: null,
    onCancel: vi.fn(),
    onCreate: vi.fn(),
    ...overrides,
  };
  render(<CreateKnowledgeBaseDialog {...props} />);
  return props;
}

test("submits trimmed knowledge-base fields", () => {
  const props = renderDialog();

  fireEvent.change(screen.getByLabelText("Knowledge base name"), {
    target: { value: "  Runbooks  " },
  });
  fireEvent.change(screen.getByLabelText("Knowledge base description"), {
    target: { value: "  Operations  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));

  expect(props.onCreate).toHaveBeenCalledWith("Runbooks", "Operations");
});

test("rejects a blank knowledge-base name without submitting", () => {
  const props = renderDialog();

  fireEvent.change(screen.getByLabelText("Knowledge base name"), {
    target: { value: "   " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));

  expect(screen.getByRole("alert")).toHaveTextContent(
    "A knowledge base name is required"
  );
  expect(props.onCreate).not.toHaveBeenCalled();
});

test("disables submission and shows safe errors", () => {
  renderDialog({ pending: true, error: "Could not create the knowledge base" });

  expect(screen.getByRole("button", { name: "Creating…" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Could not create the knowledge base"
  );
});

test("focuses the name and cancels with Escape", async () => {
  const props = renderDialog();

  await waitFor(() =>
    expect(screen.getByLabelText("Knowledge base name")).toHaveFocus()
  );
  fireEvent.keyDown(document, { key: "Escape" });

  expect(props.onCancel).toHaveBeenCalledOnce();
});

