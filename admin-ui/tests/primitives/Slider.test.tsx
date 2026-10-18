import * as React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { Slider } from "@/components/primitives/Slider";

function StatefulSlider() {
  const [value, setValue] = React.useState(25);
  return (
    <Slider
      value={value}
      min={0}
      max={100}
      step={1}
      onChange={setValue}
      aria-label="Volume"
    />
  );
}

test("Slider renders range input", () => {
  render(<StatefulSlider />);

  const slider = screen.getByRole("slider") as HTMLInputElement;
  expect(slider).toBeDefined();
  expect(slider.type).toBe("range");
  expect(slider.min).toBe("0");
  expect(slider.max).toBe("100");
  expect(slider.value).toBe("25");

  fireEvent.change(slider, { target: { value: "60" } });
  expect(slider.value).toBe("60");
});
