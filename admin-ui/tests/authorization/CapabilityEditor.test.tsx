import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import {
  GroupCapabilityEditor,
  OrganizationCapabilityEditor,
} from "@/components/authorization/CapabilityEditor";

const putOrganization = vi.fn();
const putGroup = vi.fn();

let organizationMutation = {
  mutate: putOrganization,
  isPending: false,
  isError: false,
  error: null as unknown,
};
let organizationCapabilities = ["chat.send"];

const catalogData = {
  data: [
    { key: "chat.send", label: "Send chat", description: "Start and continue chats" },
    { key: "skills.use", label: "Use skills", description: "Run assigned skills" },
    {
      key: "web.access",
      label: "Internet access",
      description: "Search and read public web sources",
    },
  ],
};

vi.mock("@/hooks/useAuthorization", () => ({
  useCapabilityCatalog: () => ({ data: catalogData, isLoading: false, error: null }),
  useOrganizationCapabilities: () => ({
    data: { data: { capabilities: organizationCapabilities } },
    isLoading: false,
    error: null,
  }),
  usePutOrganizationCapabilities: () => organizationMutation,
  useGroupCapabilities: () => ({
    data: { data: { capabilities: ["skills.use"] } },
    isLoading: false,
    error: null,
  }),
  usePutGroupCapabilities: () => ({
    mutate: putGroup,
    isPending: false,
    isError: false,
    error: null,
  }),
}));

beforeEach(() => {
  putOrganization.mockReset();
  putGroup.mockReset();
  organizationMutation = {
    mutate: putOrganization,
    isPending: false,
    isError: false,
    error: null,
  };
  organizationCapabilities = ["chat.send"];
});

test("renders the server catalog and reconciles the complete organization capability set", () => {
  render(<OrganizationCapabilityEditor />);

  // Known keys are labelled from the UI catalog, unknown ones by the server.
  expect(screen.getByText("Send chat messages")).toBeInTheDocument();
  expect(screen.getByText("Start and continue AI conversations.")).toBeInTheDocument();
  expect(screen.getByText("Access the web")).toBeInTheDocument();
  expect(screen.getByText("Use skills")).toBeInTheDocument();
  expect(screen.getByText("Run assigned skills")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: /Send chat/ })).toBeChecked();

  fireEvent.click(screen.getByRole("checkbox", { name: /Use skills/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Access the web/ }));
  fireEvent.click(screen.getByRole("button", { name: "Save organization capabilities" }));

  expect(putOrganization).toHaveBeenCalledWith(
    ["chat.send", "skills.use", "web.access"],
    expect.anything()
  );
});

test("reconciles capabilities for the selected group", () => {
  render(<GroupCapabilityEditor groupId="group-1" groupLabel="Analysts" />);

  fireEvent.click(screen.getByRole("checkbox", { name: /Send chat/ }));
  fireEvent.click(screen.getByRole("button", { name: "Save capabilities of group Analysts" }));

  expect(putGroup).toHaveBeenCalledWith(
    { groupId: "group-1", capabilities: ["chat.send", "skills.use"] },
    expect.anything()
  );
});

test("keeps the desired capability selection visible after a 403", () => {
  organizationMutation = {
    mutate: putOrganization,
    isPending: false,
    isError: true,
    error: { response: new Response(null, { status: 403 }) },
  };

  render(<OrganizationCapabilityEditor />);
  fireEvent.click(screen.getByRole("checkbox", { name: /Use skills/ }));

  expect(screen.getByRole("checkbox", { name: /Use skills/ })).toBeChecked();
  expect(screen.getByText("Access denied")).toBeInTheDocument();
});

test("keeps the success message after the mutation refreshes assigned capabilities", () => {
  putOrganization.mockImplementation(
    (_capabilities, options: { onSuccess: () => void }) => options.onSuccess()
  );
  const { rerender } = render(<OrganizationCapabilityEditor />);

  fireEvent.click(screen.getByRole("checkbox", { name: /Use skills/ }));
  fireEvent.click(
    screen.getByRole("button", { name: "Save organization capabilities" })
  );
  organizationCapabilities = ["chat.send", "skills.use"];
  rerender(<OrganizationCapabilityEditor />);

  expect(screen.getByText("Capabilities saved.")).toBeVisible();
});
