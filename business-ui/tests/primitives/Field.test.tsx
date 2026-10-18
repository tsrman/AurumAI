import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { Field } from "@/components/primitives/Field";

test("Field renders label and input", () => {
  render(<Field label="Email" placeholder="you@example.com" />);

  expect(screen.getByText("Email")).toBeDefined();
  const input = screen.getByRole("textbox") as HTMLInputElement;
  expect(input).toBeDefined();
  expect(input.placeholder).toBe("you@example.com");
});

test("Field forwards input props", () => {
  const onChange = vi.fn();
  render(<Field label="Name" value="Ada" onChange={onChange} />);

  const input = screen.getByRole("textbox") as HTMLInputElement;
  expect(input.value).toBe("Ada");
});
