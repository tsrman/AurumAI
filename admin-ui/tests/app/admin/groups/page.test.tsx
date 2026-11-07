import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import AdminGroupsPage from "@/app/admin/groups/page";
import type { AdminUser, Group, GroupMember } from "@/lib/api";

let adminAccess = { allowed: true, canWrite: true, error: null as string | null, isLoading: false };
let groupsResult: { data?: { data: Group[] }; isLoading: boolean; error: unknown } = {
  data: { data: [] },
  isLoading: false,
  error: null,
};
const createMutate = vi.fn((_payload: unknown, options?: { onSuccess?: () => void }) => options?.onSuccess?.());
const updateMutate = vi.fn();
const removeMutate = vi.fn();
let removeError: Error | null = null;
const usersQuery = vi.fn();
const membersQuery = vi.fn();
const addMembersMutate = vi.fn();
const removeMemberMutate = vi.fn();
let members: GroupMember[] = [];
let people: AdminUser[] = [];

vi.mock("next/navigation", () => ({ usePathname: () => "/admin/groups" }));
vi.mock("@/hooks/useAdminAccess", () => ({ useAdminAccess: () => adminAccess }));
vi.mock("@/components/authorization/CapabilityEditor", () => ({
  OrganizationCapabilityEditor: () => <div>organization-capability-editor</div>,
  GroupCapabilityEditor: ({ groupLabel }: { groupLabel: string }) => <div>group-capability-editor:{groupLabel}</div>,
}));
vi.mock("@/hooks/useGroups", () => ({
  useGroups: () => groupsResult,
  useCreateGroup: () => ({ mutate: createMutate, reset: vi.fn(), isPending: false, isError: false }),
  useUpdateGroup: () => ({ mutate: updateMutate, isPending: false, isError: false, isSuccess: false }),
  useDeleteGroup: () => ({ mutate: removeMutate, isPending: false, isError: Boolean(removeError), error: removeError }),
  useGroupMembers: (groupId: string, params: unknown) => {
    membersQuery(groupId, params);
    return { data: { data: members, pagination: { hasMore: false, total: members.length } }, isLoading: false, error: null };
  },
  useAddGroupMembers: () => ({ mutate: addMembersMutate, isPending: false, isError: false }),
  useRemoveGroupMember: () => ({ mutate: removeMemberMutate, isPending: false, isError: false }),
}));
vi.mock("@/hooks/useAdminAnalytics", () => ({
  useAdminUsers: (filters: unknown) => {
    usersQuery(filters);
    return { data: { data: people, pagination: { hasMore: false, total: people.length } }, isLoading: false, error: null };
  },
}));

const group: Group = { id: "group-1", name: "support", memberCount: 3 };

beforeEach(() => {
  adminAccess = { allowed: true, canWrite: true, error: null, isLoading: false };
  groupsResult = { data: { data: [] }, isLoading: false, error: null };
  createMutate.mockClear();
  updateMutate.mockClear();
  removeMutate.mockClear();
  usersQuery.mockClear();
  membersQuery.mockClear();
  addMembersMutate.mockClear();
  removeMemberMutate.mockClear();
  removeError = null;
  members = [];
  people = [];
});

function openGroup(name = "support") {
  fireEvent.click(screen.getByRole("button", { name: `Edit group ${name}` }));
  return screen.getByRole("dialog", { name: `Group “${name}”` });
}

test("shows access loading and denial states", () => {
  adminAccess = { allowed: false, canWrite: false, error: null, isLoading: true };
  const { rerender } = render(<AdminGroupsPage />);
  expect(screen.getByText("Checking access...")).toBeInTheDocument();
  adminAccess = { allowed: false, canWrite: false, error: "Administrator only", isLoading: false };
  rerender(<AdminGroupsPage />);
  expect(screen.getByText("Administrator only")).toBeInTheDocument();
});

test("creates an identity-only group from the New group dialog", () => {
  render(<AdminGroupsPage />);
  fireEvent.click(screen.getByRole("button", { name: "New group" }));
  const dialog = screen.getByRole("dialog", { name: "New group" });
  fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: " billing " } });
  fireEvent.click(within(dialog).getByRole("button", { name: "New group" }));
  expect(createMutate).toHaveBeenCalledWith({ name: "billing" }, expect.anything());
  expect(screen.queryByRole("dialog", { name: "New group" })).not.toBeInTheDocument();
});

test("lists groups with their source and member count, filtered by search", () => {
  groupsResult = {
    data: { data: [group, { id: "group-2", name: "analysts", lastSyncedAt: "2026-07-18T00:00:00Z", memberCount: 0 }] },
    isLoading: false,
    error: null,
  };
  render(<AdminGroupsPage />);
  const support = screen.getByText("support").closest("tr")!;
  expect(within(support).getByText("Created here")).toBeInTheDocument();
  expect(within(support).getByText("3")).toBeInTheDocument();
  expect(within(screen.getByText("analysts").closest("tr")!).getByText("Keycloak")).toBeInTheDocument();

  fireEvent.change(screen.getByPlaceholderText("Search groups"), { target: { value: "ANA" } });
  expect(screen.queryByText("support")).not.toBeInTheDocument();
  expect(screen.getByText("analysts")).toBeInTheDocument();
});

test("edits default permissions from the organization row", () => {
  render(<AdminGroupsPage />);
  fireEvent.click(screen.getByRole("button", { name: /Default permissions/ }));
  expect(within(screen.getByRole("dialog")).getByText("organization-capability-editor")).toBeInTheDocument();
});

test("the group dialog renames, edits permissions and lists members", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  members = [{ id: "u1", email: "ann@example.com", fullName: "Ann", role: "member", status: "active", source: "identity" }];
  render(<AdminGroupsPage />);
  const dialog = openGroup();

  fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "helpdesk" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  expect(updateMutate).toHaveBeenCalledWith({ id: "group-1", data: { name: "helpdesk" } });

  fireEvent.click(within(dialog).getByRole("tab", { name: "Permissions" }));
  expect(within(dialog).getByText("group-capability-editor:support")).toBeInTheDocument();

  fireEvent.click(within(dialog).getByRole("tab", { name: "Members" }));
  expect(membersQuery).toHaveBeenLastCalledWith("group-1", expect.objectContaining({ limit: 25, offset: 0 }));
  expect(within(dialog).getByText("Ann")).toBeInTheDocument();
});

const member = (id: string, name: string, source: GroupMember["source"]): GroupMember => ({
  id,
  email: `${name.toLowerCase()}@example.com`,
  fullName: name,
  role: "member",
  status: "active",
  source,
});

test("members added here can be removed, synchronized ones are managed in Keycloak", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  members = [member("u1", "Ann", "identity"), member("u2", "Bob", "manual")];
  render(<AdminGroupsPage />);
  const dialog = openGroup();
  fireEvent.click(within(dialog).getByRole("tab", { name: "Members" }));

  expect(within(dialog).getByText("Managed in Keycloak")).toBeInTheDocument();
  expect(within(dialog).getByText("Added here")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Remove Ann from the group" })).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Remove Bob from the group" }));
  expect(removeMemberMutate).toHaveBeenCalledWith({ groupId: "group-1", userId: "u2" });
});

test("adds the selected people to the group and skips those already in it", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  members = [member("u1", "Ann", "identity")];
  people = [
    { id: "u1", email: "ann@example.com", fullName: "Ann", role: "member", status: "active", lastLoginAt: null, createdAt: "2026-01-01T00:00:00Z" },
    { id: "u3", email: "cy@example.com", fullName: "Cy", role: "member", status: "active", lastLoginAt: null, createdAt: "2026-01-01T00:00:00Z" },
  ];
  render(<AdminGroupsPage />);
  const dialog = openGroup();
  fireEvent.click(within(dialog).getByRole("tab", { name: "Members" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Add people" }));

  const picker = within(dialog).getByRole("region", { name: "Add people" });
  fireEvent.change(within(picker).getByRole("searchbox"), { target: { value: " cy " } });
  expect(usersQuery).toHaveBeenLastCalledWith(expect.objectContaining({ search: "cy" }));
  expect(within(picker).getByRole("checkbox", { name: /Ann/ })).toBeDisabled();
  expect(within(picker).getByRole("button", { name: "Add selected (0)" })).toBeDisabled();
  fireEvent.click(within(picker).getByRole("checkbox", { name: /Cy/ }));
  fireEvent.click(within(picker).getByRole("button", { name: "Add selected (1)" }));
  expect(addMembersMutate).toHaveBeenCalledWith({ groupId: "group-1", userIds: ["u3"] }, expect.anything());
});

test("a read-only administrator sees members but cannot change them", () => {
  adminAccess = { allowed: true, canWrite: false, error: null, isLoading: false };
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  members = [member("u2", "Bob", "manual")];
  render(<AdminGroupsPage />);
  const dialog = openGroup();
  fireEvent.click(within(dialog).getByRole("tab", { name: "Members" }));
  expect(within(dialog).getByText("Bob")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Add people" })).not.toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
});

test("confirms group deletion", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  render(<AdminGroupsPage />);
  const dialog = openGroup();
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
  expect(removeMutate).toHaveBeenCalledWith("group-1", expect.anything());
});

test("explains why Keycloak-managed groups cannot be renamed or deleted locally", () => {
  groupsResult = { data: { data: [{ ...group, lastSyncedAt: "2026-10-24T00:00:00Z" }] }, isLoading: false, error: null };
  render(<AdminGroupsPage />);
  const dialog = openGroup();

  expect(within(dialog).getByLabelText("Name")).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "Delete" })).toBeDisabled();
  expect(within(dialog).getByText("Manage this group in Keycloak. Local deletion would be undone by synchronization."))
    .toBeVisible();
});

test("shows a failed deletion inside the confirmation dialog", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  const { rerender } = render(<AdminGroupsPage />);
  const dialog = openGroup();
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  removeError = new Error("Deletion failed");
  rerender(<AdminGroupsPage />);
  expect(within(screen.getByRole("alertdialog")).getByText("Deletion failed")).toBeVisible();
});

test("read-only viewers cannot create, rename or delete", () => {
  adminAccess = { allowed: true, canWrite: false, error: null, isLoading: false };
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  render(<AdminGroupsPage />);
  expect(screen.queryByRole("button", { name: "New group" })).not.toBeInTheDocument();
  const dialog = openGroup();
  expect(within(dialog).getByLabelText("Name")).toBeDisabled();
  expect(within(dialog).queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
});

test("shows group query errors", () => {
  groupsResult = { data: undefined, isLoading: false, error: new Error("boom") };
  render(<AdminGroupsPage />);
  expect(screen.getByText("boom")).toBeInTheDocument();
});
