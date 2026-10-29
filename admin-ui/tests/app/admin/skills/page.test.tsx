import { render, screen, fireEvent, within } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import AdminSkillsPage from "@/app/admin/skills/page";
import { SkillFormDialog } from "@/components/aurum/admin/SkillFormDialog";
import type { Skill } from "@/lib/api";

let adminAccess = {
  allowed: true,
  canWrite: true,
  error: null as string | null,
  isLoading: false,
};
let skillsResult: {
  data?: { data: Skill[] };
  isLoading: boolean;
  error: unknown;
} = { data: { data: [] }, isLoading: false, error: null };
let isOrgAdmin = true;

const createMutate = vi.fn((_payload: unknown, options?: { onSuccess?: () => void }) =>
  options?.onSuccess?.()
);
const updateMutate = vi.fn((_vars: unknown, options?: { onSuccess?: () => void }) =>
  options?.onSuccess?.()
);
const removeMutate = vi.fn((_id: unknown, options?: { onSuccess?: () => void }) =>
  options?.onSuccess?.()
);
const discoverMutate = vi.fn();
const discoverReset = vi.fn();
const uploadMutate = vi.fn();
const uploadReset = vi.fn();
const replaceBundleMutate = vi.fn();
const replaceBundleReset = vi.fn();

vi.mock("@/components/aurum/admin/HermesSkillDrafts", () => ({
  HermesSkillDrafts: () => null,
}));

vi.mock("@/hooks/useAdminAccess", () => ({
  useAdminAccess: () => adminAccess,
}));

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ user: { role: isOrgAdmin ? "org_admin" : "member" } }),
}));

vi.mock("@/components/authorization/GrantEditor", () => ({
  GrantEditor: ({ resourceLabel }: { resourceLabel: string }) => (
    <div>grant-editor:{resourceLabel}</div>
  ),
}));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => skillsResult,
  useCreateSkill: () => ({ mutate: createMutate, isPending: false, isError: false }),
  useUpdateSkill: () => ({ mutate: updateMutate, isPending: false, isError: false }),
  useDeleteSkill: () => ({ mutate: removeMutate, isPending: false }),
  useDiscoverMcpTools: () => ({
    mutate: discoverMutate,
    reset: discoverReset,
    isPending: false,
    isError: false,
    error: null,
    data: undefined,
  }),
  useUploadCodeSkill: () => ({
    mutate: uploadMutate,
    reset: uploadReset,
    isPending: false,
    isError: false,
    error: null,
  }),
  useReplaceCodeSkillBundle: () => ({
    mutate: replaceBundleMutate,
    reset: replaceBundleReset,
    isPending: false,
    isError: false,
    error: null,
    isSuccess: false,
  }),
}));

const skill: Skill = {
  id: "skill-1",
  name: "web_search",
  type: "tool",
  inputSchema: {},
};

test("tenant MCP form offers SSE only", () => {
  render(<SkillFormDialog open skill={null} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "mcp" }));
  expect(screen.getByRole("button", { name: "sse" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByRole("button", { name: "stdio" })).not.toBeInTheDocument();
});

test("legacy stdio requires explicit migration before saving", () => {
  render(<SkillFormDialog open skill={{ ...skill, type: "mcp", mcpTransport: "stdio" }} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Update skill" }));
  expect(updateMutate).not.toHaveBeenCalled();
  expect(screen.getByText(/Choose SSE and enter the external MCP server address/)).toBeInTheDocument();
});

test("MCP endpoint replacement does not reuse saved discovery credentials", () => {
  render(<SkillFormDialog open skill={{ ...skill, type: "mcp", mcpTransport: "sse", mcpEndpoint: "https://old.example/sse", hasMcpAuthToken: true }} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Discover tools" }));
  expect(discoverMutate).toHaveBeenLastCalledWith(expect.objectContaining({ skillId: skill.id }));
  fireEvent.change(screen.getByLabelText("MCP Endpoint"), { target: { value: "https://new.example/sse" } });
  expect(screen.getByPlaceholderText(/The address changed/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Discover tools" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Authorization token (Authorization: Bearer …)"), { target: { value: "replacement-token" } });
  fireEvent.click(screen.getByRole("button", { name: "Discover tools" }));
  expect(discoverMutate).toHaveBeenLastCalledWith(expect.objectContaining({
    endpoint: "https://new.example/sse", skillId: skill.id, authToken: "replacement-token",
  }));
});

beforeEach(() => {
  adminAccess = { allowed: true, canWrite: true, error: null, isLoading: false };
  skillsResult = { data: { data: [] }, isLoading: false, error: null };
  isOrgAdmin = true;
  createMutate.mockClear();
  updateMutate.mockClear();
  removeMutate.mockClear();
  discoverMutate.mockClear();
  discoverReset.mockClear();
  uploadMutate.mockClear();
  uploadReset.mockClear();
  replaceBundleMutate.mockClear();
  replaceBundleReset.mockClear();
});

test("shows a loading state while checking admin access", () => {
  adminAccess = { allowed: false, canWrite: false, error: null, isLoading: true };
  render(<AdminSkillsPage />);
  expect(screen.getByText("Checking access...")).toBeInTheDocument();
});

test("shows an access-denied message when not allowed", () => {
  adminAccess = {
    allowed: false,
    canWrite: false,
    error: "Access denied. The admin:read or admin:write permission is required.",
    isLoading: false,
  };
  render(<AdminSkillsPage />);
  expect(
    screen.getByText("Access denied. The admin:read or admin:write permission is required.")
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add skill" })).not.toBeInTheDocument();
});

test("renders the skill list", () => {
  skillsResult = { data: { data: [skill] }, isLoading: false, error: null };
  render(<AdminSkillsPage />);
  const row = screen.getByText("web_search").closest("tr")!;
  expect(row).toHaveTextContent("Tool");
});

test("submits a v2-only create payload", () => {
  render(<AdminSkillsPage />);

  // The empty state repeats the header action; both open the same form.
  expect(screen.getByText("No skills yet")).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "Add skill" })[0]);

  fireEvent.change(screen.getByLabelText("Name"), {
    target: { value: "hybrid_search" },
  });
  fireEvent.click(screen.getByRole("button", { name: "agent" }));
  fireEvent.click(screen.getByRole("button", { name: "Create skill" }));

  expect(createMutate).toHaveBeenCalledTimes(1);
  const [payload] = createMutate.mock.calls[0];
  expect(payload).toMatchObject({
    name: "hybrid_search",
    type: "agent",
  });
  expect(payload).not.toHaveProperty("permissions");
  // dialog closed via onSuccess
  expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
});

test("editing a skill pre-fills the form and submits an update", () => {
  skillsResult = { data: { data: [skill] }, isLoading: false, error: null };
  render(<AdminSkillsPage />);

  fireEvent.click(screen.getByRole("button", { name: "Edit web_search" }));
  expect(screen.getByLabelText("Name")).toHaveValue("web_search");
  expect(screen.getByRole("button", { name: "Update skill" })).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "renamed" } });
  fireEvent.click(screen.getByRole("button", { name: "Update skill" }));

  expect(updateMutate).toHaveBeenCalledTimes(1);
  const [vars] = updateMutate.mock.calls[0] as [{ data: Record<string, unknown> }];
  expect(vars).toMatchObject({ id: "skill-1", data: { name: "renamed" } });
});

test("deleting a skill asks for confirmation, then calls remove with its id", () => {
  skillsResult = { data: { data: [skill] }, isLoading: false, error: null };
  render(<AdminSkillsPage />);

  fireEvent.click(screen.getByRole("button", { name: "Delete web_search" }));
  expect(removeMutate).not.toHaveBeenCalled();

  const confirmDialog = screen.getByRole("alertdialog");
  fireEvent.click(within(confirmDialog).getByRole("button", { name: "Delete" }));

  expect(removeMutate).toHaveBeenCalledWith("skill-1", expect.anything());
});

test("an admin:read-only viewer sees the list but no mutation controls", () => {
  adminAccess = { allowed: true, canWrite: false, error: null, isLoading: false };
  skillsResult = { data: { data: [skill] }, isLoading: false, error: null };
  render(<AdminSkillsPage />);

  expect(screen.getByText("web_search")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add skill" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Edit web_search" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete web_search" })).not.toBeInTheDocument();
});

test("shows an error banner when the skills query fails", () => {
  skillsResult = { data: undefined, isLoading: false, error: new Error("boom") };
  render(<AdminSkillsPage />);
  expect(screen.getByText("boom")).toBeInTheDocument();
});

test("configures skill access from the row, not from the edit form", () => {
  skillsResult = { data: { data: [skill] }, isLoading: false, error: null };
  render(<AdminSkillsPage />);

  fireEvent.click(screen.getByRole("button", { name: "Edit web_search" }));
  expect(screen.queryByLabelText(/Permissions/)).not.toBeInTheDocument();
  expect(screen.queryByText("grant-editor:web_search")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Update skill" }));
  const [vars] = updateMutate.mock.calls[0] as [{ data: Record<string, unknown> }];
  expect(vars.data).not.toHaveProperty("permissions");

  fireEvent.click(screen.getByRole("button", { name: "Access to web_search" }));
  const dialog = screen.getByRole("dialog", { name: "Access to “web_search”" });
  expect(within(dialog).getByText("grant-editor:web_search")).toBeInTheDocument();
});

test("filters skills by search text and type", () => {
  skillsResult = {
    data: { data: [skill, { ...skill, id: "skill-2", name: "crm_server", type: "mcp", description: "Customer records" }] },
    isLoading: false,
    error: null,
  };
  render(<AdminSkillsPage />);

  fireEvent.click(screen.getByRole("button", { name: "MCP" }));
  expect(screen.queryByText("web_search")).not.toBeInTheDocument();
  expect(screen.getByText("crm_server")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "All" }));
  fireEvent.change(screen.getByPlaceholderText("Search skills"), { target: { value: "customer" } });
  expect(screen.queryByText("web_search")).not.toBeInTheDocument();
  expect(screen.getByText("crm_server")).toBeInTheDocument();
});

test("read-only viewers cannot open access control", () => {
  adminAccess = { allowed: true, canWrite: false, error: null, isLoading: false };
  skillsResult = { data: { data: [skill] }, isLoading: false, error: null };
  render(<AdminSkillsPage />);
  expect(screen.queryByRole("button", { name: "Access to web_search" })).not.toBeInTheDocument();
});
