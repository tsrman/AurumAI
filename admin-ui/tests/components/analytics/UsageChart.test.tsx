import { render, screen } from "@testing-library/react";

import { UsageChart } from "@/components/analytics/UsageChart";
import type { DailyUsage } from "@/lib/api";

const daily: DailyUsage[] = [
  {
    date: "2026-09-01",
    requests: 0,
    promptTokens: 0,
    completionTokens: 0,
    totalTokens: 0,
    unaccountedRequests: 0,
  },
  {
    date: "2026-09-02",
    requests: 2,
    promptTokens: 5,
    completionTokens: 3,
    totalTokens: 8,
    unaccountedRequests: 1,
  },
];

test("renders an accessible token chart and its exact source table", () => {
  render(<UsageChart daily={daily} metric="totalTokens" />);

  expect(
    screen.getByRole("img", { name: "Token usage per day" })
  ).toBeInTheDocument();
  const table = screen.getByRole("table", {
    name: "Usage chart data",
  });
  expect(table).toHaveTextContent("2026-09-01");
  expect(table).toHaveTextContent("2026-09-02");
  expect(table).toHaveTextContent("0");
  expect(table).toHaveTextContent("8");
});

test("a single day is drawn as a point, since a one-coordinate polyline is invisible", () => {
  // The default view on the 1st of any UTC month is exactly one day.
  const { container } = render(
    <UsageChart daily={[{ ...daily[0], requests: 4 }]} metric="requests" />
  );

  expect(container.querySelector("polyline")).toBeNull();
  const point = container.querySelector("circle");
  expect(point).not.toBeNull();
  expect(`${point?.getAttribute("cx")},${point?.getAttribute("cy")}`).not.toMatch(
    /NaN|Infinity/
  );
});

test("all-zero data still produces finite SVG coordinates", () => {
  const { container } = render(
    <UsageChart
      daily={daily.map((row) => ({ ...row, totalTokens: 0 }))}
      metric="totalTokens"
    />
  );

  expect(container.querySelector("polyline")?.getAttribute("points")).not.toMatch(
    /NaN|Infinity/
  );
});
