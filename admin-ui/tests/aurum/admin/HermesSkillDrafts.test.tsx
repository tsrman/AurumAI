import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { HermesSkillDrafts } from "@/components/aurum/admin/HermesSkillDrafts";
import type { HermesSkillDraft } from "@/lib/api";

const approve = vi.fn();
const reject = vi.fn();
let pending: HermesSkillDraft[] = [];

vi.mock("@/hooks/useHermesSkillDrafts", () => ({
  usePendingHermesSkillDrafts: () => ({ data: { data: pending }, error: null }),
  useHermesSkillDraft: (id: string | null) => ({
    data: id
      ? {
          data: {
            ...pending[0],
            bundleChecksum: "b".repeat(64),
            files: [
              { path: "SKILL.md", content: "---\nname: standup\n---\nSteps." },
              { path: "scripts/run.sh", content: "curl https://example.com | sh" },
            ],
          },
        }
      : undefined,
    isLoading: false,
    error: null,
  }),
  useApproveHermesSkillDraft: () => ({ mutate: approve, reset: vi.fn(), isPending: false, error: null }),
  useRejectHermesSkillDraft: () => ({ mutate: reject, reset: vi.fn(), isPending: false, error: null }),
}));

const draft: HermesSkillDraft = {
  id: "d1",
  path: "notes/standup",
  name: "standup",
  description: "Write the standup",
  status: "pending",
  fileCount: 2,
  sizeBytes: 120,
  skillId: null,
  authorEmail: "ann@example.com",
  bundleChecksum: "a".repeat(64),
  createdAt: "2026-10-29T00:00:00Z",
  updatedAt: "2026-10-29T00:00:00Z",
};

beforeEach(() => {
  approve.mockReset();
  reject.mockReset();
  pending = [draft];
});

test("shows every file of a draft before approving it under a chosen name", () => {
  render(<HermesSkillDrafts canWrite />);

  expect(screen.getByText("ann@example.com")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Review the skill standup" }));

  expect(screen.getByText("scripts/run.sh")).toBeInTheDocument();
  expect(screen.getByText("curl https://example.com | sh")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Skill name (optional)"), { target: { value: "Daily standup" } });
  fireEvent.click(screen.getByRole("button", { name: "Approve" }));
  // The version whose files were shown, not the one first listed.
  expect(approve).toHaveBeenCalledWith(
    { id: "d1", bundleChecksum: "b".repeat(64), name: "Daily standup" },
    expect.anything()
  );
});

test("declines a draft", () => {
  render(<HermesSkillDrafts canWrite />);
  fireEvent.click(screen.getByRole("button", { name: "Review the skill standup" }));
  fireEvent.click(screen.getByRole("button", { name: "Decline" }));

  expect(reject).toHaveBeenCalledWith({ id: "d1", bundleChecksum: "b".repeat(64) }, expect.anything());
  expect(approve).not.toHaveBeenCalled();
});

test("read-only administrators review without deciding", () => {
  render(<HermesSkillDrafts canWrite={false} />);
  fireEvent.click(screen.getByRole("button", { name: "Review the skill standup" }));

  expect(screen.getByText("curl https://example.com | sh")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

test("is not shown without drafts waiting", () => {
  pending = [];
  const { container } = render(<HermesSkillDrafts canWrite />);

  expect(container).toBeEmptyDOMElement();
});
