import * as React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { Toggle } from "@/components/primitives/Toggle";

function StatefulToggle() {
  const [checked, setChecked] = React.useState(false);
  return (
    <Toggle
      checked={checked}
      onChange={setChecked}
      aria-label="Enable feature"
    />
  );
}

test("Toggle toggles checked state", () => {
  render(<StatefulToggle />);

  const checkbox = screen.getByRole("checkbox") as HTMLInputElement;
  expect(checkbox.checked).toBe(false);

  fireEvent.click(checkbox);
  expect(checkbox.checked).toBe(true);

  fireEvent.click(checkbox);
  expect(checkbox.checked).toBe(false);
});
