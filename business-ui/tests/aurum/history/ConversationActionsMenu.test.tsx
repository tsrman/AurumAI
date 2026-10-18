import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { ConversationActionsMenu } from "@/components/aurum/history/ConversationActionsMenu";

function setup(props: Partial<React.ComponentProps<typeof ConversationActionsMenu>> = {}) {
  const onRename = vi.fn();
  const onDelete = vi.fn();
  render(
    <ConversationActionsMenu
      conversationTitle="Test dialog"
      isDeleting={false}
      onRename={onRename}
      onDelete={onDelete}
      {...props}
    />
  );
  return { onRename, onDelete };
}

test("opens the menu on trigger click, portaled into document.body", () => {
  setup();
  fireEvent.click(screen.getByLabelText("Actions"));

  const menu = screen.getByRole("menu", { name: "Conversation actions" });
  expect(menu.parentElement).toBe(document.body);
  expect(menu.className).toMatch(/\bfixed\b/);
  expect(screen.getByRole("menuitem", { name: /Rename/i })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: /Delete/i })).toBeInTheDocument();
});

test("calls onRename and closes the menu", () => {
  const { onRename } = setup();
  fireEvent.click(screen.getByLabelText("Actions"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Rename/i }));

  expect(onRename).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

test("delete goes through a ConfirmDialog instead of window.confirm", () => {
  const { onDelete } = setup();
  fireEvent.click(screen.getByLabelText("Actions"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Delete/i }));

  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  const dialog = screen.getByRole("alertdialog", { name: /Delete the conversation\?/i });
  expect(dialog).toHaveTextContent("Test dialog");
  expect(onDelete).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(onDelete).toHaveBeenCalledTimes(1);
});

test("cancelling the delete confirm dialog does not call onDelete", () => {
  const { onDelete } = setup();
  fireEvent.click(screen.getByLabelText("Actions"));
  fireEvent.click(screen.getByRole("menuitem", { name: /Delete/i }));

  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  expect(onDelete).not.toHaveBeenCalled();
});

test("opens upward when there is no room below the trigger", () => {
  setup();
  const trigger = screen.getByLabelText("Actions");

  Object.defineProperty(window, "innerHeight", { configurable: true, value: 400 });
  trigger.getBoundingClientRect = () =>
    ({
      top: 380,
      bottom: 400,
      left: 10,
      right: 40,
      width: 30,
      height: 20,
      x: 10,
      y: 380,
      toJSON() {},
    }) as DOMRect;

  fireEvent.click(trigger);

  const menu = screen.getByRole("menu");
  expect(parseFloat(menu.style.top)).toBeLessThan(380);
});

test("uses the menu's real measured height, not zero, when deciding to flip upward", () => {
  // Regression: jsdom's offsetHeight is always 0, so a naive "spaceBelow <
  // menuHeight" test can pass by accident even when the component reads
  // the height of an element that was never actually mounted (e.g. a
  // render gated on `menuOpen && menuPosition`, where menuPosition is
  // still null on the very first measurement). Mock a *non-zero* height so
  // this test only passes if the component genuinely measures it.
  const offsetHeightSpy = vi
    .spyOn(HTMLElement.prototype, "offsetHeight", "get")
    .mockReturnValue(82);

  setup();
  const trigger = screen.getByLabelText("Actions");

  // Room for a 0-height menu (50px) but NOT for the real 82px-tall one.
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 320 });
  trigger.getBoundingClientRect = () =>
    ({
      top: 250,
      bottom: 270,
      left: 10,
      right: 40,
      width: 30,
      height: 20,
      x: 10,
      y: 250,
      toJSON() {},
    }) as DOMRect;

  fireEvent.click(trigger);

  const menu = screen.getByRole("menu");
  const top = parseFloat(menu.style.top);
  expect(top).toBeLessThan(250);
  expect(top + 82).toBeLessThanOrEqual(320);

  offsetHeightSpy.mockRestore();
});

test("closes the menu on Escape", () => {
  setup();
  fireEvent.click(screen.getByLabelText("Actions"));
  expect(screen.getByRole("menu")).toBeInTheDocument();

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

test("disables the trigger and shows a spinner while deleting", () => {
  setup({ isDeleting: true });
  const trigger = screen.getByLabelText("Actions");
  expect(trigger).toBeDisabled();
  expect(document.querySelector("svg.animate-spin")).toBeInTheDocument();
});
