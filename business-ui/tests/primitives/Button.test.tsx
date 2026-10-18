import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { Button } from "@/components/primitives/Button";

test("Button renders and calls onClick", () => {
  const onClick = vi.fn();
  render(<Button onClick={onClick}>Send</Button>);

  const button = screen.getByRole("button", { name: "Send" });
  expect(button).toBeDefined();
  expect(button.textContent).toBe("Send");

  fireEvent.click(button);
  expect(onClick).toHaveBeenCalled();
});
