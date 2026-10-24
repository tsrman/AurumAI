import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import { AdminSidebar } from "@/components/shell/AdminSidebar";

const logout = vi.hoisted(() => vi.fn());
let pathname = "/admin/groups";

vi.mock("next/navigation", () => ({ usePathname: () => pathname }));
vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ logout, user: { fullName: "Ann Admin", role: "org_admin" } }),
}));
vi.mock("@/lib/runtime-config", () => ({ getRuntimeConfig: () => ({ appUrl: "https://chat.example.test" }) }));
vi.mock("@/components/aurum/ThemeToggle", () => ({ ThemeToggle: () => <button type="button">Theme</button> }));
vi.mock("@/components/LanguageSwitcher", () => ({ LanguageSwitcher: () => <select aria-label="Language" /> }));

beforeEach(() => {
  pathname = "/admin/groups";
  logout.mockClear();
  window.localStorage.clear();
});

test("links every admin section and the cross-origin chat", () => {
  render(<AdminSidebar />);
  const nav = screen.getByRole("navigation", { name: "Administration sections" });
  const hrefs = Object.fromEntries(
    within(nav).getAllByRole("link").map((link) => [link.textContent, link.getAttribute("href")])
  );
  expect(hrefs).toEqual({
    "Users": "/admin/users",
    Agents: "/admin/agents",
    "Skills and tools": "/admin/skills",
    Workflows: "/admin/workflows",
    "Knowledge bases": "/admin/knowledge",
    Models: "/admin/models",
    Analytics: "/admin/analytics",
  });
  expect(screen.getByRole("link", { name: "Back to chat" })).toHaveAttribute("href", "https://chat.example.test");
});

test("marks the section of nested routes as current", () => {
  pathname = "/admin/agents/agent-1";
  const { rerender } = render(<AdminSidebar />);
  expect(screen.getByRole("link", { name: "Agents" })).toHaveAttribute("aria-current", "page");
  pathname = "/admin/groups";
  rerender(<AdminSidebar />);
  expect(screen.getByRole("link", { name: "Users" })).toHaveAttribute("aria-current", "page");
});

test("collapses to icons, remembers it, and signs out", () => {
  render(<AdminSidebar />);
  fireEvent.click(screen.getByRole("button", { name: "Collapse panel" }));
  expect(screen.getByRole("link", { name: "Agents" })).toHaveAttribute("title", "Agents");
  expect(window.localStorage.getItem("aurum-admin-sidebar-collapsed")).toBe("1");
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(logout).toHaveBeenCalledOnce();
});
