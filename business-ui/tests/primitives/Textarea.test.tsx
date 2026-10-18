import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { Textarea } from "@/components/primitives/Textarea";

test("Textarea renders label and textarea", () => {
  render(<Textarea label="Description" placeholder="Add details" />);

  expect(screen.getByText("Description")).toBeDefined();
  const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
  expect(textarea).toBeDefined();
  expect(textarea.placeholder).toBe("Add details");
});

test("Textarea forwards textarea props", () => {
  const onChange = vi.fn();
  render(<Textarea label="Bio" value="Hello" onChange={onChange} />);

  const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
  expect(textarea.value).toBe("Hello");
});
