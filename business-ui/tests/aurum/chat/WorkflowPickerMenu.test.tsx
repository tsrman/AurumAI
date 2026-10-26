import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { WorkflowPickerMenu } from "@/components/aurum/chat/WorkflowPickerMenu";
import { useChatAgents } from "@/hooks/useChatAgents";
import { useWorkflows } from "@/hooks/useWorkflows";

vi.mock("@/hooks/useWorkflows", () => ({ useWorkflows: vi.fn() }));
vi.mock("@/hooks/useChatAgents", () => ({ useChatAgents: vi.fn() }));

const CHAT = { kind: "chat" } as const;

function offerHermes(offered: boolean) {
  vi.mocked(useChatAgents).mockReturnValue({
    data: offered ? [{ id: "hermes" }] : [],
  } as ReturnType<typeof useChatAgents>);
}

beforeEach(() => {
  vi.mocked(useWorkflows).mockReturnValue({
    data: [
      {
        id: "workflow-1",
        name: "Research",
        description: "Parallel analysis",
        version: 1,
        maxParallelism: 2,
        status: "active",
      },
    ],
    isLoading: false,
  } as ReturnType<typeof useWorkflows>);
  offerHermes(false);
});

test("offers ordinary chat and readable workflows", () => {
  render(<WorkflowPickerMenu value={CHAT} onChange={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));

  expect(screen.getByRole("option", { name: /Plain chat/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /Research/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Hermes Agent/ })).not.toBeInTheDocument();
});

test("selects a workflow and can return to ordinary chat", () => {
  const onChange = vi.fn();
  const { rerender } = render(
    <WorkflowPickerMenu value={CHAT} onChange={onChange} />
  );
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Research/ }));
  expect(onChange).toHaveBeenCalledWith({ kind: "workflow", workflowId: "workflow-1" });

  rerender(
    <WorkflowPickerMenu
      value={{ kind: "workflow", workflowId: "workflow-1" }}
      onChange={onChange}
    />
  );
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Plain chat/ }));
  expect(onChange).toHaveBeenLastCalledWith(CHAT);
});

test("offers Hermes Agent when the backend accepts its turns", () => {
  offerHermes(true);
  const onChange = vi.fn();
  const { rerender } = render(<WorkflowPickerMenu value={CHAT} onChange={onChange} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Hermes Agent/ }));
  expect(onChange).toHaveBeenCalledWith({ kind: "agent", agent: "hermes" });

  rerender(
    <WorkflowPickerMenu value={{ kind: "agent", agent: "hermes" }} onChange={onChange} />
  );
  expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent(
    "Hermes Agent"
  );
});

test("is disabled without authenticated runtime", () => {
  render(<WorkflowPickerMenu value={CHAT} onChange={vi.fn()} disabled />);
  expect(screen.getByRole("button", { name: "Choose workflow" })).toBeDisabled();
});
