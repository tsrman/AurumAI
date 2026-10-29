import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { HermesSkillDraftsSection } from "@/components/aurum/settings/HermesSkillDraftsSection";
import { useChatAgents } from "@/hooks/useChatAgents";
import {
  type HermesSkillDraft,
  useHermesSkillDrafts,
  useWithdrawHermesSkillDraft,
} from "@/hooks/useHermesSkillDrafts";

vi.mock("@/hooks/useChatAgents", () => ({ useChatAgents: vi.fn() }));
vi.mock("@/hooks/useHermesSkillDrafts", () => ({
  useHermesSkillDrafts: vi.fn(),
  useWithdrawHermesSkillDraft: vi.fn(),
}));

const mutate = vi.fn();

function draft(id: string, name: string, status: HermesSkillDraft["status"]): HermesSkillDraft {
  return {
    id,
    path: `notes/${name}`,
    name,
    description: `About ${name}`,
    status,
    fileCount: 1,
    sizeBytes: 100,
    skillId: null,
    createdAt: "2026-10-29T00:00:00Z",
    updatedAt: "2026-10-29T00:00:00Z",
  };
}

function given(drafts: HermesSkillDraft[] | undefined, hermes = true) {
  vi.mocked(useChatAgents).mockReturnValue({
    data: hermes ? [{ id: "hermes" }] : [],
  } as ReturnType<typeof useChatAgents>);
  vi.mocked(useHermesSkillDrafts).mockReturnValue({
    data: drafts,
    isError: false,
  } as ReturnType<typeof useHermesSkillDrafts>);
  vi.mocked(useWithdrawHermesSkillDraft).mockReturnValue({
    mutate,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useWithdrawHermesSkillDraft>);
}

beforeEach(() => mutate.mockReset());

test("lists the skills Hermes wrote and withdraws only one still waiting", () => {
  given([draft("d1", "standup", "pending"), draft("d2", "digest", "approved")]);
  render(<HermesSkillDraftsSection />);

  expect(screen.getByText("Waiting for review")).toBeInTheDocument();
  expect(screen.getByText("Approved")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Withdraw the skill digest" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Withdraw the skill standup" }));
  expect(mutate).toHaveBeenCalledWith("d1");
});

test("says so when Hermes wrote no skills", () => {
  given([]);
  render(<HermesSkillDraftsSection />);

  expect(screen.getByText("Hermes Agent has written no skills yet.")).toBeInTheDocument();
});

test("is not shown where Hermes cannot be used", () => {
  given(undefined, false);
  const { container } = render(<HermesSkillDraftsSection />);

  expect(container).toBeEmptyDOMElement();
});
