import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { Pill } from "@/components/primitives/Pill";

test("Pill renders in active state", () => {
  render(<Pill active>Active</Pill>);
  const pill = screen.getByRole("button", { name: "Active" });
  expect(pill.className.includes("bg-aurum-accent-soft")).toBe(true);
  expect(pill.className.includes("border-aurum-accent")).toBe(true);
});

test("Pill calls onClick", () => {
  const onClick = vi.fn();
  render(<Pill onClick={onClick}>Click me</Pill>);

  const pill = screen.getByRole("button", { name: "Click me" });
  fireEvent.click(pill);
  expect(onClick).toHaveBeenCalled();
});
