import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";

test("renders nothing when closed", () => {
  render(
    <ConfirmDialog
      open={false}
      title="Delete the conversation?"
      onConfirm={vi.fn()}
      onCancel={vi.fn()}
    />
  );

  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});

test("renders via a portal into document.body when open", () => {
  render(
    <ConfirmDialog
      open
      title="Delete the conversation?"
      description="“Test” will be deleted permanently."
      onConfirm={vi.fn()}
      onCancel={vi.fn()}
    />
  );

  const dialog = screen.getByRole("alertdialog", { name: "Delete the conversation?" });
  expect(dialog.closest("body")).toBe(document.body);
  expect(screen.getByText(/will be deleted permanently/)).toBeInTheDocument();
});

test("calls onConfirm when the confirm button is clicked", () => {
  const onConfirm = vi.fn();
  render(
    <ConfirmDialog
      open
      title="Delete the conversation?"
      confirmLabel="Delete"
      onConfirm={onConfirm}
      onCancel={vi.fn()}
    />
  );

  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(onConfirm).toHaveBeenCalledTimes(1);
});

test("calls onCancel when the cancel button is clicked", () => {
  const onCancel = vi.fn();
  render(
    <ConfirmDialog
      open
      title="Delete the conversation?"
      cancelLabel="Cancel"
      onConfirm={vi.fn()}
      onCancel={onCancel}
    />
  );

  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("calls onCancel when the backdrop is clicked", () => {
  const onCancel = vi.fn();
  const { container } = render(
    <ConfirmDialog
      open
      title="Delete the conversation?"
      onConfirm={vi.fn()}
      onCancel={onCancel}
    />
  );
  // The dialog is portaled to document.body, not `container`.
  void container;

  const backdrop = document.querySelector('[aria-hidden="true"]');
  expect(backdrop).not.toBeNull();
  fireEvent.click(backdrop!);
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("does not call onCancel when clicking inside the dialog card", () => {
  const onCancel = vi.fn();
  render(
    <ConfirmDialog
      open
      title="Delete the conversation?"
      onConfirm={vi.fn()}
      onCancel={onCancel}
    />
  );

  fireEvent.click(screen.getByRole("alertdialog"));
  expect(onCancel).not.toHaveBeenCalled();
});

test("calls onCancel on Escape", () => {
  const onCancel = vi.fn();
  render(
    <ConfirmDialog
      open
      title="Delete the conversation?"
      onConfirm={vi.fn()}
      onCancel={onCancel}
    />
  );

  fireEvent.keyDown(document, { key: "Escape" });
  expect(onCancel).toHaveBeenCalledTimes(1);
});

test("focuses the confirm button when opened", () => {
  render(
    <ConfirmDialog
      open
      title="Delete the conversation?"
      confirmLabel="Delete"
      onConfirm={vi.fn()}
      onCancel={vi.fn()}
    />
  );

  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "Delete" })
  );
});
