import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import KnowledgePage from "@/app/knowledge/page";
import type { KnowledgeBase } from "@/types/aurum";

const push = vi.fn();
const createBase = vi.fn();
let authorizationReady = true;
let canCreate = true;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({
    authorizationReady,
    hasCapability: (capability: string) =>
      canCreate && capability === "workspace.knowledge.create",
  }),
}));

vi.mock("@/hooks/useKnowledge", () => ({
  useKnowledge: () => ({
    knowledgeBases: [],
    createBase,
    deleteBase: vi.fn(),
    isLoading: false,
    isCreating: false,
    isDeleting: false,
  }),
}));

const createdBase: KnowledgeBase = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Runbooks",
  badge: "RU",
  badgeColor: "amber",
  description: "Operations",
  canWrite: true,
  docCount: 0,
  size: "—",
  updated: "03.09.2026",
  status: "indexed",
  docs: [],
};

beforeEach(() => {
  authorizationReady = true;
  canCreate = true;
  push.mockReset();
  createBase.mockReset().mockResolvedValue(createdBase);
});

test.each([
  [false, true],
  [true, false],
])(
  "hides creation until authorization is ready and capability is granted",
  (ready, granted) => {
    authorizationReady = ready;
    canCreate = granted;

    render(<KnowledgePage />);

    expect(
      screen.queryByRole("button", { name: "Create knowledge base" })
    ).not.toBeInTheDocument();
  }
);

test("creates a base and navigates to its detail page", async () => {
  render(<KnowledgePage />);

  fireEvent.click(screen.getByRole("button", { name: "Create knowledge base" }));
  fireEvent.change(screen.getByLabelText("Knowledge base name"), {
    target: { value: "Runbooks" },
  });
  fireEvent.change(screen.getByLabelText("Knowledge base description"), {
    target: { value: "Operations" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));

  await waitFor(() => expect(createBase).toHaveBeenCalledWith("Runbooks", "Operations"));
  expect(push).toHaveBeenCalledWith(
    "/knowledge/11111111-1111-1111-1111-111111111111"
  );
});

test("keeps the dialog open with a safe creation error", async () => {
  createBase.mockRejectedValue(new TypeError("network detail"));
  render(<KnowledgePage />);

  fireEvent.click(screen.getByRole("button", { name: "Create knowledge base" }));
  fireEvent.change(screen.getByLabelText("Knowledge base name"), {
    target: { value: "Runbooks" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Network error");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(push).not.toHaveBeenCalled();
});
