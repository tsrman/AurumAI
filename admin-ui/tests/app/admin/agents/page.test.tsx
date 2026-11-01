import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import AdminAgentsPage from "@/app/admin/agents/page";
import type { AgentDefinition } from "@/lib/api";

let adminAccess = { allowed: true, canWrite: true, error: null as string | null, isLoading: false };
let agentsResult: { data?: { data: AgentDefinition[] }; isLoading: boolean; error: unknown };
const push = vi.fn();
let lastIncludeArchived: boolean | undefined;
const archiveMutate = vi.fn((_vars: { id: string; archived: boolean }, options?: { onSuccess?: () => void }) =>
  options?.onSuccess?.(),
);
const removeMutate = vi.fn((_id: string, options?: { onSuccess?: () => void }) => options?.onSuccess?.());

vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/hooks/useAdminAccess", () => ({ useAdminAccess: () => adminAccess }));
vi.mock("@/components/authorization/GrantEditor", () => ({
  GrantEditor: ({ resourceType, resourceLabel }: { resourceType: string; resourceLabel: string }) => (
    <div>grant-editor:{resourceType}:{resourceLabel}</div>
  ),
}));
vi.mock("@/hooks/useAgentDefinitions", () => ({
  useAgentDefinitions: (includeArchived?: boolean) => {
    lastIncludeArchived = includeArchived;
    return agentsResult;
  },
  useArchiveAgentDefinition: () => ({ mutate: archiveMutate, reset: vi.fn(), isPending: false, isError: false, error: null }),
  useDeleteAgentDefinition: () => ({ mutate: removeMutate, reset: vi.fn(), isPending: false, isError: false, error: null }),
}));

const agent: AgentDefinition = {
  id: "agent-1",
  name: "Researcher",
  description: "Finds sources",
  modelId: "provider/model",
  systemPrompt: "Be precise",
  temperature: null,
  skillIds: ["s1", "s2"],
  knowledgeBaseIds: ["k1"],
  canWrite: true,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-02-01T00:00:00Z",
};

beforeEach(() => {
  adminAccess = { allowed: true, canWrite: true, error: null, isLoading: false };
  agentsResult = { data: { data: [agent] }, isLoading: false, error: null };
  push.mockClear();
  removeMutate.mockClear();
  archiveMutate.mockClear();
  lastIncludeArchived = undefined;
});

test("lists agents with model and dependencies", () => {
  render(<AdminAgentsPage />);
  const row = screen.getByText("Researcher").closest("tr")!;
  expect(within(row).getByText("provider/model")).toBeInTheDocument();
  expect(within(row).getByText("2 skills · 1 base")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "New agent" })).toHaveAttribute("href", "/admin/agents/new");
});

test("opens the editor, access control and delete confirmation from a row", () => {
  render(<AdminAgentsPage />);
  fireEvent.click(screen.getByRole("button", { name: "Edit Researcher" }));
  expect(push).toHaveBeenCalledWith("/admin/agents/agent-1");

  fireEvent.click(screen.getByRole("button", { name: "Access to Researcher" }));
  expect(screen.getByText("grant-editor:agent_definition:Researcher")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));

  fireEvent.click(screen.getByRole("button", { name: "Delete Researcher" }));
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
  expect(removeMutate).toHaveBeenCalledWith("agent-1", expect.anything());
});

test("agents the administrator cannot write stay read-only", () => {
  agentsResult = { data: { data: [{ ...agent, canWrite: false }] }, isLoading: false, error: null };
  render(<AdminAgentsPage />);
  expect(screen.queryByRole("button", { name: "Edit Researcher" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete Researcher" })).not.toBeInTheDocument();
});

test("shows the empty state with a create action and filters by search", () => {
  const { rerender } = render(<AdminAgentsPage />);
  fireEvent.change(screen.getByPlaceholderText("Search agents"), { target: { value: "nothing" } });
  expect(screen.getByText("No agents match the search")).toBeInTheDocument();

  agentsResult = { data: { data: [] }, isLoading: false, error: null };
  rerender(<AdminAgentsPage />);
  fireEvent.change(screen.getByPlaceholderText("Search agents"), { target: { value: "" } });
  expect(screen.getByText("No agents yet")).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "New agent" })).toHaveLength(2);
});

test("archives an agent after confirmation and explains what that means", () => {
  render(<AdminAgentsPage />);
  fireEvent.click(screen.getByRole("button", { name: "Archive Researcher" }));
  const dialog = screen.getByRole("alertdialog");
  expect(within(dialog).getByText(/Past runs keep their history/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Archive" }));
  expect(archiveMutate).toHaveBeenCalledWith({ id: "agent-1", archived: true }, expect.anything());
});

test("archived agents are fetched on demand, badged and restorable", () => {
  render(<AdminAgentsPage />);
  expect(lastIncludeArchived).toBe(false);
  fireEvent.click(screen.getByRole("checkbox", { name: "Show archived" }));
  expect(lastIncludeArchived).toBe(true);

  agentsResult = { data: { data: [{ ...agent, archivedAt: "2026-03-01T00:00:00Z" }] }, isLoading: false, error: null };
  render(<AdminAgentsPage />);
  const row = screen.getAllByText("Researcher").map((el) => el.closest("tr")!).find((tr) => within(tr).queryByText("Archived"))!;
  expect(within(row).queryByRole("button", { name: "Edit Researcher" })).not.toBeInTheDocument();
  fireEvent.click(within(row).getByRole("button", { name: "Restore Researcher" }));
  expect(archiveMutate).toHaveBeenCalledWith({ id: "agent-1", archived: false });
});
