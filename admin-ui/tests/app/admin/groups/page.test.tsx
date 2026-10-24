import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import AdminGroupsPage from "@/app/admin/groups/page";
import type { Group } from "@/lib/api";

let adminAccess = { allowed: true, canWrite: true, error: null as string | null, isLoading: false };
let groupsResult: { data?: { data: Group[] }; isLoading: boolean; error: unknown } = {
  data: { data: [] },
  isLoading: false,
  error: null,
};
const createMutate = vi.fn((_payload: unknown, options?: { onSuccess?: () => void }) => options?.onSuccess?.());
const removeMutate = vi.fn();
let removeError: Error | null = null;

vi.mock("@/hooks/useAdminAccess", () => ({ useAdminAccess: () => adminAccess }));
vi.mock("@/components/authorization/CapabilityEditor", () => ({
  OrganizationCapabilityEditor: () => <div>organization-capability-editor</div>,
  GroupCapabilityEditor: ({ groupLabel }: { groupLabel: string }) => <div>group-capability-editor:{groupLabel}</div>,
}));
vi.mock("@/hooks/useGroups", () => ({
  useGroups: () => groupsResult,
  useCreateGroup: () => ({ mutate: createMutate, isPending: false, isError: false }),
  useDeleteGroup: () => ({ mutate: removeMutate, isPending: false, isError: Boolean(removeError), error: removeError }),
}));

const group: Group = { id: "group-1", name: "support" };

beforeEach(() => {
  adminAccess = { allowed: true, canWrite: true, error: null, isLoading: false };
  groupsResult = { data: { data: [] }, isLoading: false, error: null };
  createMutate.mockClear();
  removeMutate.mockClear();
  removeError = null;
});

test("shows access loading and denial states", () => {
  adminAccess = { allowed: false, canWrite: false, error: null, isLoading: true };
  const { rerender } = render(<AdminGroupsPage />);
  expect(screen.getByText("Checking access...")).toBeInTheDocument();
  adminAccess = { allowed: false, canWrite: false, error: "Administrator only", isLoading: false };
  rerender(<AdminGroupsPage />);
  expect(screen.getByText("Administrator only")).toBeInTheDocument();
});

test("creates an identity-only group without a legacy permission field", () => {
  render(<AdminGroupsPage />);
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "billing" } });
  fireEvent.click(screen.getByRole("button", { name: "Create group" }));
  expect(createMutate).toHaveBeenCalledWith({ name: "billing" }, expect.anything());
  expect(screen.queryByLabelText(/Permissions/)).not.toBeInTheDocument();
});

test("renders group identity and Keycloak sync state only", () => {
  groupsResult = {
    data: { data: [group, { id: "group-2", name: "analysts", lastSyncedAt: "2026-07-18T00:00:00Z" }] },
    isLoading: false,
    error: null,
  };
  render(<AdminGroupsPage />);
  expect(within(screen.getByText("support").closest("tr")!).queryByText("Keycloak")).not.toBeInTheDocument();
  expect(within(screen.getByText("analysts").closest("tr")!).getByText("Keycloak")).toBeInTheDocument();
  expect(screen.queryByText("Permissions")).not.toBeInTheDocument();
});

test("opens the capability editor for a group", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  render(<AdminGroupsPage />);
  expect(screen.getByText("organization-capability-editor")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Configure capabilities support" }));
  expect(screen.getByText("group-capability-editor:support")).toBeInTheDocument();
});

test("confirms group deletion", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  render(<AdminGroupsPage />);
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
  expect(removeMutate).toHaveBeenCalledWith("group-1", expect.anything());
});

test("explains why Keycloak-managed groups cannot be deleted locally", () => {
  groupsResult = { data: { data: [{ ...group, lastSyncedAt: "2026-10-24T00:00:00Z" }] }, isLoading: false, error: null };
  render(<AdminGroupsPage />);

  expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  expect(screen.getByText("Manage this group in Keycloak. Local deletion would be undone by synchronization."))
    .toBeVisible();
});

test("shows a failed deletion inside the confirmation dialog", () => {
  groupsResult = { data: { data: [group] }, isLoading: false, error: null };
  const { rerender } = render(<AdminGroupsPage />);
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  removeError = new Error("Deletion failed");
  // The mutation failed while the confirmation remains open.
  rerender(<AdminGroupsPage />);
  expect(within(screen.getByRole("alertdialog")).getByText("Deletion failed")).toBeVisible();
});

test("shows group query errors", () => {
  groupsResult = { data: undefined, isLoading: false, error: new Error("boom") };
  render(<AdminGroupsPage />);
  expect(screen.getByText("boom")).toBeInTheDocument();
});
