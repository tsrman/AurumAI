import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import { DeleteKnowledgeBaseButton } from "@/components/aurum/knowledge/DeleteKnowledgeBaseButton";

const deleteBase = vi.fn();

vi.mock("@/hooks/useKnowledge", () => ({
  useKnowledge: () => ({ deleteBase, isDeleting: false }),
}));

beforeEach(() => {
  deleteBase.mockReset().mockResolvedValue(undefined);
});

function renderButton(
  overrides: Partial<React.ComponentProps<typeof DeleteKnowledgeBaseButton>> = {}
) {
  const props: React.ComponentProps<typeof DeleteKnowledgeBaseButton> = {
    knowledgeBaseId: "kb-1",
    knowledgeBaseName: "Runbooks",
    disabled: false,
    onDeleted: vi.fn(),
    ...overrides,
  };
  render(<DeleteKnowledgeBaseButton {...props} />);
  return props;
}

test("deletes only after explaining that documents must be removed first", async () => {
  const props = renderButton();

  fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
  expect(screen.getByRole("alertdialog")).toHaveTextContent(
    "Delete all documents first"
  );
  expect(deleteBase).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  await waitFor(() => expect(deleteBase).toHaveBeenCalledWith("kb-1"));
  expect(props.onDeleted).toHaveBeenCalledOnce();
});

test("keeps confirmation open and renders a safe conflict", async () => {
  deleteBase.mockRejectedValue({ response: new Response(null, { status: 409 }) });
  const props = renderButton();

  fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Delete the related data first"
  );
  expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  expect(props.onDeleted).not.toHaveBeenCalled();
});

test("blocks duplicate confirmation while deletion is pending", async () => {
  let resolveDelete!: () => void;
  deleteBase.mockReturnValue(
    new Promise<void>((resolve) => {
      resolveDelete = resolve;
    })
  );
  renderButton();

  fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Deleting…" })).toBeDisabled()
  );
  await act(async () => {
    resolveDelete();
    await Promise.resolve();
  });
});

test("respects the non-empty-base disabled state", () => {
  renderButton({ disabled: true });

  expect(
    screen.getByRole("button", { name: "Delete knowledge base" })
  ).toBeDisabled();
});
