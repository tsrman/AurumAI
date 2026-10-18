import { render, screen } from "@testing-library/react";
import { Badge } from "@/components/primitives/Badge";
import type { BadgeColor } from "@/types/aurum";

const colors: BadgeColor[] = ["amber", "green", "blue", "violet", "rose"];

test("Badge renders with each color", () => {
  const { rerender } = render(<Badge color="amber">Label</Badge>);

  for (const color of colors) {
    rerender(<Badge color={color}>{color}</Badge>);
    const badge = screen.getByText(color);
    expect(badge.className.includes(`bg-c-${color}-bg`)).toBe(true);
    expect(badge.className.includes(`text-c-${color}-fg`)).toBe(true);
  }
});

test("Badge forwards native span props", () => {
  render(
    <Badge color="amber" data-testid="my-badge">
      X
    </Badge>
  );
  expect(screen.getByTestId("my-badge").getAttribute("data-testid")).toBe(
    "my-badge"
  );
});
