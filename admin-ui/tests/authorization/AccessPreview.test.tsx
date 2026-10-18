import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { AccessPreview } from "@/components/authorization/AccessPreview";

const previewMutate = vi.fn();
let previewState = {
  mutate: previewMutate,
  isPending: false,
  isError: false,
  error: null as unknown,
  data: undefined as
    | {
        data: {
          subjectType: "user" | "group";
          subjectId: string;
          adminBypass: boolean;
          capabilities: string[];
          resources: Array<{
            resourceType: "skill";
            resourceId: string;
            canRead: boolean;
            canWrite: boolean;
            source: string;
          }>;
        };
      }
    | undefined,
};

vi.mock("@/hooks/useAuthorization", () => ({
  useAccessPreview: () => previewState,
}));

beforeEach(() => {
  previewMutate.mockReset();
  previewState = {
    mutate: previewMutate,
    isPending: false,
    isError: false,
    error: null,
    data: undefined,
  };
});

test("requests preview for a selected user and explains every access source", () => {
  previewState.data = {
    data: {
      subjectType: "user",
      subjectId: "user-1",
      adminBypass: false,
      capabilities: ["chat.send"],
      resources: [
        {
          resourceType: "skill",
          resourceId: "skill-1",
          canRead: true,
          canWrite: false,
          source: "grant",
        },
        {
          resourceType: "skill",
          resourceId: "skill-2",
          canRead: true,
          canWrite: true,
          source: "admin_bypass",
        },
        {
          resourceType: "skill",
          resourceId: "skill-3",
          canRead: false,
          canWrite: false,
          source: "none",
        },
      ],
    },
  };

  render(
    <AccessPreview
      subject={{ principalType: "user", principalId: "user-1", label: "Alice" }}
      resources={[
        { resourceType: "skill", resourceId: "skill-1", label: "Search" },
        { resourceType: "skill", resourceId: "skill-2", label: "Code" },
        { resourceType: "skill", resourceId: "skill-3", label: "Private" },
      ]}
    />
  );

  fireEvent.click(screen.getByRole("button", { name: "Check effective access" }));
  expect(previewMutate).toHaveBeenCalledWith({
    subjectType: "user",
    subjectId: "user-1",
    resources: [
      { resourceType: "skill", resourceId: "skill-1" },
      { resourceType: "skill", resourceId: "skill-2" },
      { resourceType: "skill", resourceId: "skill-3" },
    ],
  });
  expect(screen.getByText("Assigned by a grant")).toBeInTheDocument();
  expect(screen.getByText("Organization administrator access")).toBeInTheDocument();
  expect(screen.getByText("No matching grant")).toBeInTheDocument();
  expect(screen.getByText("chat.send")).toBeInTheDocument();
});

test("shows a forbidden response instead of hiding the server denial", () => {
  previewState = {
    mutate: previewMutate,
    isPending: false,
    isError: true,
    error: { response: new Response(null, { status: 403 }) },
    data: undefined,
  };

  render(
    <AccessPreview
      subject={{ principalType: "group", principalId: "group-1", label: "Analysts" }}
      resources={[{ resourceType: "skill", resourceId: "skill-1", label: "Search" }]}
    />
  );

  expect(screen.getByText("Access denied")).toBeInTheDocument();
});
