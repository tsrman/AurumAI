import * as React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { SegmentedControl } from "@/components/primitives/SegmentedControl";

const options = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
];

function StatefulSegmentedControl() {
  const [value, setValue] = React.useState("day");
  return (
    <SegmentedControl
      options={options}
      value={value}
      onChange={setValue}
    />
  );
}

test("SegmentedControl renders options and tracks active selection", () => {
  render(<StatefulSegmentedControl />);

  const group = screen.getByRole("group");
  expect(group).toBeDefined();

  const buttons = screen.getAllByRole("button");
  expect(buttons.length).toBe(3);

  const dayButton = screen.getByRole("button", { name: "Day" });
  expect(dayButton.getAttribute("aria-pressed")).toBe("true");
  expect(dayButton.getAttribute("type")).toBe("button");

  const weekButton = screen.getByRole("button", { name: "Week" });
  expect(weekButton.getAttribute("aria-pressed")).toBe("false");

  fireEvent.click(weekButton);
  expect(weekButton.getAttribute("aria-pressed")).toBe("true");
  expect(dayButton.getAttribute("aria-pressed")).toBe("false");
});
