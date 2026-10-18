import { afterEach, expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";

afterEach(() => vi.unstubAllEnvs());

test("About page offers the operator's source URL and local project license", async () => {
  vi.stubEnv("RUNTIME_SOURCE_CODE_URL", "https://example.org/operator/source");
  const pageModule = await import("@/app/about/page");
  render(<pageModule.default />);
  expect(screen.getByRole("heading", { name: "AurumAI" })).toBeVisible();
  expect(screen.getByRole("link", { name: "Source code" })).toHaveAttribute("href", "https://example.org/operator/source");
  expect(screen.getByRole("link", { name: "AGPL-3.0-only license" })).toHaveAttribute("href", "/LICENSE");
});

test("About page does not invent a repository URL before configuration", async () => {
  vi.stubEnv("RUNTIME_SOURCE_CODE_URL", "");
  const pageModule = await import("@/app/about/page");
  render(<pageModule.default />);
  expect(screen.queryByRole("link", { name: "Source code" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "AGPL-3.0-only license" })).toBeVisible();
});
