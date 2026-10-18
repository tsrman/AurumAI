import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { AdminNav } from "@/components/AdminNav";

const logout = vi.hoisted(() => vi.fn());
vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ logout }),
}));

vi.mock("@/lib/runtime-config.server", () => ({
  getServerRuntimeConfig: () => ({ appUrl: "https://chat.example.test" }),
}));
vi.mock("@/components/aurum/ThemeToggle", () => ({
  ThemeToggle: () => <button type="button">Theme</button>,
}));
vi.mock("@/components/LanguageSwitcher", () => ({
  LanguageSwitcher: () => <select aria-label="Language" />,
}));

test("keeps every admin destination and the cross-origin chat link", () => {
  render(<AdminNav />);

  expect(screen.getByRole("link", { name: /To chat/ })).toHaveAttribute(
    "href",
    "https://chat.example.test"
  );
  expect(screen.getByRole("link", { name: "Groups" })).toHaveAttribute(
    "href",
    "/admin/groups"
  );
  expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute(
    "href",
    "/admin/models"
  );
  expect(screen.getByRole("link", { name: "Access" })).toHaveAttribute(
    "href",
    "/admin/access"
  );
  expect(screen.getByRole("link", { name: "Workflows" })).toHaveAttribute(
    "href",
    "/admin/workflows"
  );
  expect(screen.getByRole("link", { name: "Analytics" })).toHaveAttribute(
    "href",
    "/admin/analytics"
  );
  expect(screen.getByRole("link", { name: "Agents / Skills" })).toHaveAttribute(
    "href",
    "/admin/agents"
  );
  expect(screen.getByRole("link", { name: "Constructor" })).toHaveAttribute(
    "href",
    "/agents/constructor"
  );
});

test("lets the administrator end the authenticated session from navigation", () => {
  render(<AdminNav />);
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(logout).toHaveBeenCalledOnce();
});
