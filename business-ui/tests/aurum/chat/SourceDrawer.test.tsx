import { render, screen, fireEvent } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import { SourceDrawer } from "@/components/aurum/chat/SourceDrawer";
import { AppShellProvider } from "@/providers/AppShellProvider";
import type { Source } from "@/types/aurum";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: false, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));

function Wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(AppShellProvider, null, children);
}

const source: Source = {
  index: 1,
  doc: "Regulation TR-04",
  section: "cl. 4.3",
  score: 94,
  knowledgeBase: "Regulations and instructions",
  snippet: "Keep the slurry pH within 10.5–11.0.",
};

test("renders nothing when drawer source is null", () => {
  const { container } = render(React.createElement(SourceDrawer), {
    wrapper: Wrapper,
  });
  expect(container.firstChild).toBeNull();
});

test("opens drawer with dialog role and focusable close button", () => {
  render(
    React.createElement(
      AppShellProvider,
      { initialState: { drawerSource: source } },
      React.createElement(SourceDrawer)
    )
  );

  const dialog = screen.getByRole("dialog");
  expect(dialog).toBeInTheDocument();
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog).toHaveAttribute("aria-labelledby", "source-drawer-title");
  expect(screen.getByText("Regulation TR-04")).toBeInTheDocument();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
});

test("closes drawer on Escape", () => {
  render(
    React.createElement(
      AppShellProvider,
      { initialState: { drawerSource: source } },
      React.createElement(SourceDrawer)
    )
  );

  expect(screen.getByRole("dialog")).toBeInTheDocument();

  fireEvent.keyDown(document, { key: "Escape" });

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
