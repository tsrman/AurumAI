import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import AdminKnowledgePage from "@/app/admin/knowledge/page";

let adminAccess = { allowed: true, canWrite: true, error: null as string | null, isLoading: false };

vi.mock("@/hooks/useAdminAccess", () => ({ useAdminAccess: () => adminAccess }));
vi.mock("@/lib/runtime-config", () => ({ getRuntimeConfig: () => ({ appUrl: "https://chat.example.test" }) }));
vi.mock("@/components/authorization/GrantEditor", () => ({
  GrantEditor: ({ resourceType, resourceLabel }: { resourceType: string; resourceLabel: string }) => (
    <div>grant-editor:{resourceType}:{resourceLabel}</div>
  ),
}));
vi.mock("@/hooks/useKnowledgeBases", () => ({
  useKnowledgeBases: () => ({
    data: {
      data: [
        { id: "kb-1", name: "Handbook", description: "HR rules", canWrite: true, createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-05T00:00:00Z" },
      ],
    },
    isLoading: false,
    error: null,
  }),
}));

beforeEach(() => {
  adminAccess = { allowed: true, canWrite: true, error: null, isLoading: false };
});

test("links documents to the chat app and configures access here", () => {
  render(<AdminKnowledgePage />);
  expect(screen.getByRole("link", { name: "Manage documents" })).toHaveAttribute("href", "https://chat.example.test/knowledge");
  const row = screen.getByText("Handbook").closest("tr")!;
  expect(within(row).getByRole("link", { name: "Open Handbook in the chat app" })).toHaveAttribute(
    "href",
    "https://chat.example.test/knowledge/kb-1"
  );
  fireEvent.click(within(row).getByRole("button", { name: "Access to Handbook" }));
  expect(screen.getByText("grant-editor:knowledge_base:Handbook")).toBeInTheDocument();
});

test("read-only viewers get no access control", () => {
  adminAccess = { allowed: true, canWrite: false, error: null, isLoading: false };
  render(<AdminKnowledgePage />);
  expect(screen.queryByRole("button", { name: "Access to Handbook" })).not.toBeInTheDocument();
});
