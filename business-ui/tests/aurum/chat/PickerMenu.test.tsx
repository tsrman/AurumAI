import { render, screen, fireEvent, act } from "@testing-library/react";
import { vi } from "vitest";
import { PickerMenu } from "@/components/aurum/chat/PickerMenu";

const items = [
  { id: "a", label: "Alpha", description: "First option" },
  { id: "b", label: "Beta", description: "Second option" },
  { id: "c", label: "Gamma" },
];

function setup(props = {}) {
  const onSelect = vi.fn();
  render(
    <PickerMenu
      ariaLabel="Test picker"
      trigger={<button type="button">Open</button>}
      items={items}
      activeId="a"
      onSelect={onSelect}
      {...props}
    />
  );
  return { onSelect };
}

test("opens on trigger click", () => {
  setup();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  expect(screen.getByRole("listbox")).toBeInTheDocument();
  expect(screen.getAllByRole("option")).toHaveLength(items.length);
});

test("closes on Escape", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));
  expect(screen.getByRole("listbox")).toBeInTheDocument();

  fireEvent.keyDown(document, { key: "Escape" });

  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
});

test("arrow keys move focus", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  expect(document.activeElement).toBe(options[0]);

  fireEvent.keyDown(options[0], { key: "ArrowDown" });
  expect(document.activeElement).toBe(options[1]);

  fireEvent.keyDown(options[1], { key: "ArrowDown" });
  expect(document.activeElement).toBe(options[2]);

  fireEvent.keyDown(options[2], { key: "ArrowUp" });
  expect(document.activeElement).toBe(options[1]);
});

test("selecting item calls onSelect and closes menu", () => {
  const { onSelect } = setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  fireEvent.click(options[1]);

  expect(onSelect).toHaveBeenCalledTimes(1);
  expect(onSelect).toHaveBeenCalledWith("b");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
});

test("active item has aria-selected true", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  expect(options[0]).toHaveAttribute("aria-selected", "true");
  expect(options[1]).toHaveAttribute("aria-selected", "false");
  expect(options[2]).toHaveAttribute("aria-selected", "false");
});

test("focuses selected option on open", () => {
  setup({ activeId: "b" });
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  expect(document.activeElement).toBe(options[1]);
});

test("focuses first option when activeId is not found", () => {
  setup({ activeId: "unknown" });
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  expect(document.activeElement).toBe(options[0]);
});

test("closes menu when focus leaves to an outside element", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  const outside = document.createElement("button");
  document.body.appendChild(outside);
  fireEvent.blur(options[0], { relatedTarget: outside });

  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  document.body.removeChild(outside);
});

test("does not close menu when focus moves from option to trigger", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const trigger = screen.getByRole("button", { name: /Open/i });
  const options = screen.getAllByRole("option");
  fireEvent.blur(options[0], { relatedTarget: trigger });

  expect(screen.getByRole("listbox")).toBeInTheDocument();
});

test("returns focus to trigger after Escape", () => {
  setup();
  const trigger = screen.getByRole("button", { name: /Open/i });
  fireEvent.click(trigger);

  fireEvent.keyDown(document, { key: "Escape" });

  expect(document.activeElement).toBe(trigger);
});

test("returns focus to trigger after selecting an option", () => {
  setup();
  const trigger = screen.getByRole("button", { name: /Open/i });
  fireEvent.click(trigger);

  const options = screen.getAllByRole("option");
  fireEvent.click(options[1]);

  expect(document.activeElement).toBe(trigger);
});

test("does not return focus to trigger when focus leaves via Tab", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  const outside = document.createElement("button");
  document.body.appendChild(outside);
  act(() => {
    outside.focus();
  });
  fireEvent.blur(options[0], { relatedTarget: outside });

  expect(document.activeElement).toBe(outside);
  document.body.removeChild(outside);
});

test("renders the menu via a portal into document.body, positioned fixed", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const menu = screen.getByRole("listbox");
  // Regression: the menu used to be an in-flow "absolute" child of the
  // trigger's wrapper, which ran off-screen below the viewport when the
  // trigger sat near the bottom of the window (e.g. the chat composer).
  expect(menu.parentElement).toBe(document.body);
  // Tailwind's "fixed" utility (position:fixed) — jsdom doesn't load the
  // stylesheet, so assert via the class rather than computed style.
  expect(menu.className).toMatch(/\bfixed\b/);
});

test("opens upward instead of running off-screen when there is no room below the trigger", () => {
  setup();
  const trigger = screen.getByRole("button", { name: /Open/i });

  // Simulate a trigger pinned near the bottom of a short viewport (like the
  // chat composer), leaving no room for the menu to open downward.
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 400 });
  trigger.getBoundingClientRect = () =>
    ({
      top: 380,
      bottom: 400,
      left: 10,
      right: 100,
      width: 90,
      height: 20,
      x: 10,
      y: 380,
      toJSON() {},
    }) as DOMRect;

  fireEvent.click(trigger);

  const menu = screen.getByRole("listbox");
  expect(parseFloat(menu.style.top)).toBeLessThan(380);
});

test("opens downward when there is plenty of room below the trigger", () => {
  setup();
  const trigger = screen.getByRole("button", { name: /Open/i });

  Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
  trigger.getBoundingClientRect = () =>
    ({
      top: 20,
      bottom: 40,
      left: 10,
      right: 100,
      width: 90,
      height: 20,
      x: 10,
      y: 20,
      toJSON() {},
    }) as DOMRect;

  fireEvent.click(trigger);

  const menu = screen.getByRole("listbox");
  expect(parseFloat(menu.style.top)).toBeGreaterThan(40);
});

test("caps menu height and enables scrolling for long lists instead of running off-screen unreachably", () => {
  // Regression: with no max-height/overflow, a long list (e.g. a real
  // provider's full model list) just grew past the viewport edge with no
  // scrollbar — options past the bottom were not merely awkward to reach,
  // they had no scroll path to reach at all.
  const manyItems = Array.from({ length: 30 }, (_, i) => ({
    id: `item-${i}`,
    label: `Model ${i}`,
  }));
  const onSelect = vi.fn();
  render(
    <PickerMenu
      ariaLabel="Long picker"
      trigger={<button type="button">Open</button>}
      items={manyItems}
      activeId="item-0"
      onSelect={onSelect}
    />
  );

  Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const menu = screen.getByRole("listbox");
  expect(menu.className).toMatch(/\boverflow-y-auto\b/);
  expect(menu.style.maxHeight).not.toBe("");
  expect(parseFloat(menu.style.maxHeight)).toBeGreaterThan(0);
  expect(parseFloat(menu.style.maxHeight)).toBeLessThanOrEqual(600);
  // All items still exist in the DOM (just scrollable), so keyboard
  // navigation and the "focused option scrolls into view" browser default
  // can still reach every one of them.
  expect(screen.getAllByRole("option")).toHaveLength(manyItems.length);
});

test("positions the menu using the true unclamped content height, not the height clamped by a stale max-height", () => {
  // Regression: the component used to read menu.offsetHeight to decide
  // where to place the box — but offsetHeight is already clamped by
  // whatever max-height CSS applied on the *previous* render (including the
  // 320px placeholder on first mount), so for a genuinely tall list it
  // massively understated the content size. That positioned the box
  // assuming it would end up much shorter than the max-height it was about
  // to receive, so the real (properly capped) box overflowed past the
  // bottom of the viewport — reproduced live via Playwright against a real
  // 25-item model list (positioned ~340px too low, box bottom edge ~300px
  // past the viewport). offsetHeight is left at jsdom's default 0 here
  // (simulating "previously clamped small"); scrollHeight is mocked to a
  // realistic tall-content value the fix must use instead.
  const scrollHeightSpy = vi
    .spyOn(HTMLElement.prototype, "scrollHeight", "get")
    .mockReturnValue(900);

  setup();
  const trigger = screen.getByRole("button", { name: /Open/i });

  Object.defineProperty(window, "innerHeight", { configurable: true, value: 720 });
  trigger.getBoundingClientRect = () =>
    ({
      top: 687.5,
      bottom: 721.5,
      left: 615,
      right: 1126,
      width: 511,
      height: 34,
      x: 615,
      y: 687.5,
      toJSON() {},
    }) as DOMRect;

  fireEvent.click(trigger);

  const menu = screen.getByRole("listbox");
  const top = parseFloat(menu.style.top);
  const maxHeight = parseFloat(menu.style.maxHeight);
  // Content (900) exceeds maxHeight, so the rendered box is exactly
  // maxHeight tall — its bottom edge must land at (or above) gap-px short
  // of the trigger, not hundreds of pixels past the viewport.
  expect(top + maxHeight).toBeLessThanOrEqual(687.5 - 8 + 0.5);
  expect(top + maxHeight).toBeLessThanOrEqual(720);

  scrollHeightSpy.mockRestore();
});

test("does not return focus to trigger when focus leaves via Shift+Tab", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: /Open/i }));

  const options = screen.getAllByRole("option");
  const outside = document.createElement("button");
  document.body.appendChild(outside);
  act(() => {
    outside.focus();
  });
  fireEvent.blur(options[0], { relatedTarget: outside, shiftKey: true });

  expect(document.activeElement).toBe(outside);
  document.body.removeChild(outside);
});
