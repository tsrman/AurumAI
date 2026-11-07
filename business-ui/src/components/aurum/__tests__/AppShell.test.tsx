import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { AppShell } from "../AppShell";
import { useAuth } from "@/providers/AuthProvider";
import { usePathname, useRouter } from "next/navigation";

vi.mock("@/providers/AuthProvider");
vi.mock("next/navigation", () => ({ useRouter: vi.fn(), usePathname: vi.fn() }));
vi.mock("../Sidebar", () => ({ Sidebar: () => <aside aria-label="Sidebar">sidebar</aside> }));

function auth(isAuthenticated: boolean, isLoading: boolean) {
  vi.mocked(useAuth).mockReturnValue({ isAuthenticated, isLoading } as unknown as ReturnType<typeof useAuth>);
}

describe("AppShell", () => {
  beforeEach(() => {
    vi.mocked(useRouter).mockReturnValue({ push: vi.fn() } as unknown as ReturnType<typeof useRouter>);
    vi.mocked(usePathname).mockReturnValue("/login");
  });

  it("shows no sidebar to a visitor who has not signed in", () => {
    auth(false, false);
    render(<AppShell><p>sign in</p></AppShell>);
    expect(screen.getByText("sign in")).toBeInTheDocument();
    expect(screen.queryByLabelText("Sidebar")).not.toBeInTheDocument();
  });

  it("shows the sidebar to a signed-in user", () => {
    vi.mocked(usePathname).mockReturnValue("/chat");
    auth(true, false);
    render(<AppShell><p>chat</p></AppShell>);
    expect(screen.getByLabelText("Sidebar")).toBeInTheDocument();
  });

  it("keeps the sidebar while the session is still being resolved", () => {
    vi.mocked(usePathname).mockReturnValue("/chat");
    auth(false, true);
    render(<AppShell><p>chat</p></AppShell>);
    expect(screen.getByLabelText("Sidebar")).toBeInTheDocument();
  });
});
