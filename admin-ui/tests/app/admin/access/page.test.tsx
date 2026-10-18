import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import AdminAccessPage from "@/app/admin/access/page";
import type { AccessResourceOption } from "@/hooks/useAccessResources";
import type { ResourceType } from "@/lib/api";

let access = {
  allowed: true,
  canWrite: true,
  isLoading: false,
  error: null as string | null,
};
let query: {
  data?: AccessResourceOption[];
  isLoading: boolean;
  error: unknown;
} = {
  data: [],
  isLoading: false,
  error: null,
};
const resources: AccessResourceOption[] = [
  { type: "model", id: "first-id", label: "First" },
  { type: "model", id: "second-id", label: "Second" },
];
const { useAccessResourcesMock } = vi.hoisted(() => ({
  useAccessResourcesMock: vi.fn(),
}));
useAccessResourcesMock.mockImplementation(
  (_type: ResourceType, _options?: { enabled?: boolean }) => query
);

vi.mock("@/hooks/useAdminAccess", () => ({ useAdminAccess: () => access }));
vi.mock("@/hooks/useAccessResources", () => ({
  useAccessResources: useAccessResourcesMock,
}));
vi.mock("@/components/authorization/GrantEditor", () => ({
  GrantEditor: ({
    resourceType,
    resourceId,
    resourceLabel,
  }: {
    resourceType: string;
    resourceId: string;
    resourceLabel: string;
  }) => (
    <div data-testid="grant-editor">
      {resourceType}:{resourceId}:{resourceLabel}
    </div>
  ),
}));

beforeEach(() => {
  access = { allowed: true, canWrite: true, isLoading: false, error: null };
  query = { data: [], isLoading: false, error: null };
  useAccessResourcesMock.mockClear();
});

test("fails closed for a non-admin and disables the catalog query", () => {
  access = {
    allowed: false,
    canWrite: false,
    isLoading: false,
    error: "Administrator only",
  };

  render(<AdminAccessPage />);

  expect(screen.getByText("Administrator only")).toBeInTheDocument();
  expect(useAccessResourcesMock).toHaveBeenCalledWith("model", {
    enabled: false,
  });
  expect(
    screen.queryByRole("heading", { name: "Resource access" })
  ).not.toBeInTheDocument();
});

test("renders loading, error and empty catalog states", () => {
  query = { data: undefined, isLoading: true, error: null };
  const { rerender } = render(<AdminAccessPage />);
  expect(screen.getByText("Loading resources...")).toBeInTheDocument();

  query = {
    data: undefined,
    isLoading: false,
    error: new Error("catalog failed"),
  };
  rerender(<AdminAccessPage />);
  expect(screen.getByText("catalog failed")).toBeInTheDocument();

  query = { data: [], isLoading: false, error: null };
  rerender(<AdminAccessPage />);
  expect(
    screen.getByText("No resources of this type were found.")
  ).toBeInTheDocument();
});

test("filters by label and id, resets selection on type change, and wires GrantEditor", () => {
  query = { data: resources, isLoading: false, error: null };
  render(<AdminAccessPage />);

  fireEvent.change(screen.getByLabelText("Search resources"), {
    target: { value: "second-id" },
  });
  expect(screen.getByRole("option", { name: /Second/ })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Resource"), {
    target: { value: "second-id" },
  });
  expect(screen.getByText("second-id", { exact: true })).toBeInTheDocument();
  expect(screen.getByTestId("grant-editor")).toHaveTextContent(
    "model:second-id:Second"
  );

  fireEvent.change(screen.getByLabelText("Resource type"), {
    target: { value: "workflow" },
  });
  expect(screen.queryByTestId("grant-editor")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Search resources")).toHaveValue("");
});
