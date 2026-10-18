import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { IconButton } from "@/components/primitives/IconButton";

test("IconButton renders with aria-label", () => {
  const onClick = vi.fn();
  render(
    <IconButton aria-label="Close" onClick={onClick}>
      ×
    </IconButton>
  );

  const button = screen.getByRole("button", { name: "Close" });
  expect(button).toBeDefined();
  expect(button.getAttribute("type")).toBe("button");

  fireEvent.click(button);
  expect(onClick).toHaveBeenCalled();
});

test("IconButton renders without explicit aria-label when children are provided", () => {
  render(<IconButton>visible text</IconButton>);
  expect(screen.getByRole("button", { name: "visible text" })).toBeDefined();
});
