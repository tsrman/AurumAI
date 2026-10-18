import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { WorkflowPickerMenu } from "@/components/aurum/chat/WorkflowPickerMenu";
import { useWorkflows } from "@/hooks/useWorkflows";

vi.mock("@/hooks/useWorkflows", () => ({ useWorkflows: vi.fn() }));

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
});

test("offers ordinary chat and readable workflows", () => {
  render(<WorkflowPickerMenu value={null} onChange={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));

  expect(screen.getByRole("option", { name: /Plain chat/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /Research/ })).toBeInTheDocument();
});

test("selects a workflow and can return to ordinary chat", () => {
  const onChange = vi.fn();
  const { rerender } = render(
    <WorkflowPickerMenu value={null} onChange={onChange} />
  );
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Research/ }));
  expect(onChange).toHaveBeenCalledWith("workflow-1");

  rerender(<WorkflowPickerMenu value="workflow-1" onChange={onChange} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Plain chat/ }));
  expect(onChange).toHaveBeenLastCalledWith(null);
});

test("is disabled without authenticated runtime", () => {
  render(<WorkflowPickerMenu value={null} onChange={vi.fn()} disabled />);
  expect(screen.getByRole("button", { name: "Choose workflow" })).toBeDisabled();
});
