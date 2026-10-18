import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import { GrantEditor } from "@/components/authorization/GrantEditor";

const reconcileMutate = vi.fn();
let subjectsLoading = false;
let grantsLoading = false;
let grantsFetching = false;

let reconcileState = {
  mutate: reconcileMutate,
  isPending: false,
  isError: false,
  error: null as unknown,
};

const subjectsData = {
  data: [
    { principalType: "organization" as const, principalId: "org-1", label: "Gold Corp" },
    { principalType: "group" as const, principalId: "group-1", label: "Analysts" },
    { principalType: "user" as const, principalId: "user-1", label: "Alice" },
  ],
};

let grantsResponse: { data: typeof grantRows; meta?: { requestId: string } };
const grantRows = [
    {
      id: "grant-1",
      resourceType: "skill" as const,
      resourceId: "skill-1",
      principalType: "organization" as const,
      principalId: "org-1",
      permission: "read" as const,
      createdBy: null,
      createdVia: "migration" as const,
    },
];

vi.mock("@/hooks/useAuthorization", () => ({
  useAccessPreview: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    data: null,
  }),
  useAuthorizationSubjects: () => ({
    data: subjectsData,
    isLoading: subjectsLoading,
    error: null,
  }),
  useResourceGrants: () => ({
    data: grantsResponse,
    isLoading: grantsLoading,
    isFetching: grantsLoading || grantsFetching,
    error: null,
  }),
  useReconcileGrants: () => reconcileState,
}));

beforeEach(() => {
  reconcileMutate.mockReset();
  subjectsLoading = false;
  grantsLoading = false;
  grantsFetching = false;
  grantsResponse = { data: grantRows, meta: { requestId: "first" } };
  reconcileState = {
    mutate: reconcileMutate,
    isPending: false,
    isError: false,
    error: null,
  };
});

test("blocks full reconcile until subjects and current grants finish loading", () => {
  subjectsLoading = true;
  grantsLoading = true;

  render(
    <GrantEditor
      resourceType="skill"
      resourceId="skill-1"
      resourceLabel="Web search"
    />
  );

  expect(screen.getByText("Loading grants...")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Add grant" })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Save grants" })
  ).toBeDisabled();
});

test("renders organization, group and user principals with read/write selection", () => {
  render(
    <GrantEditor resourceType="skill" resourceId="skill-1" resourceLabel="Web search" />
  );

  const principal = screen.getByLabelText("Recipient of the new grant");
  expect(within(principal).getByRole("option", { name: "Organization: Gold Corp" })).toBeInTheDocument();
  expect(within(principal).getByRole("option", { name: "Group: Analysts" })).toBeInTheDocument();
  expect(within(principal).getByRole("option", { name: "User: Alice" })).toBeInTheDocument();
  expect(screen.getByLabelText("Access level of the new grant")).toHaveValue("read");
  expect(screen.getByText("Gold Corp")).toBeInTheDocument();
});

test("confirms and submits the complete desired grant set", () => {
  render(
    <GrantEditor resourceType="skill" resourceId="skill-1" resourceLabel="Web search" />
  );

  fireEvent.change(screen.getByLabelText("Recipient of the new grant"), {
    target: { value: "user:user-1" },
  });
  fireEvent.change(screen.getByLabelText("Access level of the new grant"), {
    target: { value: "write" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add grant" }));
  fireEvent.click(screen.getByRole("button", { name: "Save grants" }));

  const dialog = screen.getByRole("alertdialog");
  expect(dialog).toHaveTextContent("Web search");
  fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));

  expect(reconcileMutate).toHaveBeenCalledWith(
    {
      resourceType: "skill",
      resourceId: "skill-1",
      grants: [
        { principalType: "organization", principalId: "org-1", permission: "read" },
        { principalType: "user", principalId: "user-1", permission: "write" },
      ],
    },
    expect.anything()
  );
});

test("keeps the desired rows visible when the server rejects reconciliation", () => {
  reconcileState = {
    mutate: reconcileMutate,
    isPending: false,
    isError: true,
    error: { response: new Response(null, { status: 403 }) },
  };

  render(
    <GrantEditor resourceType="skill" resourceId="skill-1" resourceLabel="Web search" />
  );

  expect(screen.getByText("Gold Corp")).toBeInTheDocument();
  expect(screen.getByText("Access denied")).toBeInTheDocument();
});

test("keeps an unsaved removal when a background refetch returns unchanged grants", () => {
  const { rerender } = render(
    <GrantEditor resourceType="skill" resourceId="skill-1" resourceLabel="Web search" />
  );

  fireEvent.click(screen.getByRole("button", { name: "Remove grant Gold Corp" }));
  // React Query keeps the unchanged grant array but every response carries new meta.
  grantsResponse = { data: grantRows, meta: { requestId: "refetch" } };
  rerender(
    <GrantEditor resourceType="skill" resourceId="skill-1" resourceLabel="Web search" />
  );

  expect(screen.queryByText("Gold Corp", { selector: "span" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save grants" }));
  fireEvent.click(
    within(screen.getByRole("alertdialog")).getByRole("button", { name: "Apply" })
  );
  expect(reconcileMutate).toHaveBeenCalledWith(
    { resourceType: "skill", resourceId: "skill-1", grants: [] },
    expect.anything()
  );
});

test("blocks grant edits while current grants are refetching", () => {
  grantsFetching = true;

  render(
    <GrantEditor resourceType="skill" resourceId="skill-1" resourceLabel="Web search" />
  );

  expect(screen.getByRole("button", { name: "Remove grant Gold Corp" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Add grant" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save grants" })).toBeDisabled();
});
