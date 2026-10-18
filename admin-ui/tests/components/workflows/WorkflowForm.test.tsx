import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { WorkflowForm } from "@/components/workflows/WorkflowForm";
import type { AgentDefinition } from "@/lib/api";

const agent = (id: string, name: string): AgentDefinition => ({
  id,
  name,
  modelId: "provider/model",
  systemPrompt: "prompt",
  temperature: 0.2,
  skillIds: [],
  knowledgeBaseIds: [],
  canWrite: true,
  createdAt: "2026-09-02T00:00:00Z",
  updatedAt: "2026-09-02T00:00:00Z",
});

test("validates before submitting the exact structured DAG", async () => {
  const onValidate = vi.fn().mockResolvedValue(undefined);
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(
    <WorkflowForm
      agents={[agent("supervisor", "Supervisor"), agent("legal-agent", "Legal agent"), agent("risk-agent", "Risk agent")]}
      onValidate={onValidate}
      onSubmit={onSubmit}
    />
  );

  fireEvent.change(screen.getByLabelText("Workflow name"), { target: { value: "Contract review" } });
  fireEvent.change(screen.getByLabelText("Workflow description"), {
    target: { value: "Review contract clauses and risks" },
  });
  fireEvent.change(screen.getByLabelText("Supervisor"), { target: { value: "supervisor" } });
  fireEvent.change(screen.getByLabelText("Parallelism"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("Node 1 key"), { target: { value: "legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 name"), { target: { value: "Legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 agent"), { target: { value: "legal-agent" } });
  fireEvent.change(screen.getByLabelText("Node 1 instruction"), { target: { value: "Review clauses" } });
  fireEvent.click(screen.getByRole("button", { name: "Add node" }));
  fireEvent.change(screen.getByLabelText("Node 2 key"), { target: { value: "risk" } });
  fireEvent.change(screen.getByLabelText("Node 2 name"), { target: { value: "Risk" } });
  fireEvent.change(screen.getByLabelText("Node 2 agent"), { target: { value: "risk-agent" } });
  fireEvent.change(screen.getByLabelText("Node 2 instruction"), { target: { value: "Assess risk" } });
  fireEvent.click(screen.getByLabelText("Output handle — node Legal"));
  fireEvent.click(screen.getByLabelText("Input handle — node Risk"));

  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Validate" }));
  await waitFor(() => expect(onValidate).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({
    name: "Contract review",
    description: "Review contract clauses and risks",
    supervisorAgentDefinitionId: "supervisor",
    maxParallelism: 2,
    autoRoutable: false,
    nodes: [
      { nodeKey: "legal", displayName: "Legal", agentDefinitionId: "legal-agent", instruction: "Review clauses", positionX: 0, positionY: 0 },
      { nodeKey: "risk", displayName: "Risk", agentDefinitionId: "risk-agent", instruction: "Assess risk", positionX: 280, positionY: 0 },
    ],
    edges: [{ sourceNodeKey: "legal", targetNodeKey: "risk", condition: "on_success" }],
  }));
});

test("selecting on_failure for a dependency includes it on submit", async () => {
  const onValidate = vi.fn().mockResolvedValue(undefined);
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(
    <WorkflowForm
      agents={[agent("supervisor", "Supervisor"), agent("legal-agent", "Legal agent"), agent("risk-agent", "Risk agent")]}
      onValidate={onValidate}
      onSubmit={onSubmit}
    />
  );

  fireEvent.change(screen.getByLabelText("Workflow name"), { target: { value: "Contract review" } });
  fireEvent.change(screen.getByLabelText("Supervisor"), { target: { value: "supervisor" } });
  fireEvent.change(screen.getByLabelText("Node 1 key"), { target: { value: "legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 name"), { target: { value: "Legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 agent"), { target: { value: "legal-agent" } });
  fireEvent.change(screen.getByLabelText("Node 1 instruction"), { target: { value: "Review clauses" } });
  fireEvent.click(screen.getByRole("button", { name: "Add node" }));
  fireEvent.change(screen.getByLabelText("Node 2 key"), { target: { value: "fallback" } });
  fireEvent.change(screen.getByLabelText("Node 2 name"), { target: { value: "Fallback" } });
  fireEvent.change(screen.getByLabelText("Node 2 agent"), { target: { value: "risk-agent" } });
  fireEvent.change(screen.getByLabelText("Node 2 instruction"), { target: { value: "Handle failure" } });
  fireEvent.click(screen.getByLabelText("Output handle — node Legal"));
  fireEvent.click(screen.getByLabelText("Input handle — node Fallback"));
  fireEvent.change(await screen.findByLabelText("Link condition legal → fallback"), {
    target: { value: "on_failure" },
  });

  fireEvent.click(screen.getByRole("button", { name: "Validate" }));
  await waitFor(() => expect(onValidate).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() =>
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        edges: [{ sourceNodeKey: "legal", targetNodeKey: "fallback", condition: "on_failure" }],
      })
    )
  );
});

test("duplicate node keys disable validation and save", () => {
  render(<WorkflowForm agents={[agent("a", "A")]} onValidate={vi.fn()} onSubmit={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Add node" }));
  fireEvent.change(screen.getByLabelText("Node 2 key"), { target: { value: "node_1" } });

  expect(screen.getByText("Node keys must be unique.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
});

test("toggling the confirmation checkbox includes requiresConfirmation on submit", async () => {
  const onValidate = vi.fn().mockResolvedValue(undefined);
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(
    <WorkflowForm
      agents={[agent("supervisor", "Supervisor"), agent("legal-agent", "Legal agent")]}
      onValidate={onValidate}
      onSubmit={onSubmit}
    />
  );

  fireEvent.change(screen.getByLabelText("Workflow name"), { target: { value: "Contract review" } });
  fireEvent.change(screen.getByLabelText("Supervisor"), { target: { value: "supervisor" } });
  fireEvent.change(screen.getByLabelText("Node 1 key"), { target: { value: "legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 name"), { target: { value: "Legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 agent"), { target: { value: "legal-agent" } });
  fireEvent.change(screen.getByLabelText("Node 1 instruction"), { target: { value: "Review clauses" } });
  fireEvent.click(screen.getByLabelText("Requires confirmation — node 1"));

  fireEvent.click(screen.getByRole("button", { name: "Validate" }));
  await waitFor(() => expect(onValidate).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() =>
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        nodes: [
          expect.objectContaining({ nodeKey: "legal", requiresConfirmation: true }),
        ],
      })
    )
  );
});

test("toggling auto-routing includes autoRoutable on submit", async () => {
  const onValidate = vi.fn().mockResolvedValue(undefined);
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(
    <WorkflowForm
      agents={[agent("supervisor", "Supervisor"), agent("legal-agent", "Legal agent")]}
      onValidate={onValidate}
      onSubmit={onSubmit}
    />
  );

  fireEvent.change(screen.getByLabelText("Workflow name"), { target: { value: "Contract review" } });
  fireEvent.change(screen.getByLabelText("Supervisor"), { target: { value: "supervisor" } });
  fireEvent.change(screen.getByLabelText("Node 1 key"), { target: { value: "legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 name"), { target: { value: "Legal" } });
  fireEvent.change(screen.getByLabelText("Node 1 agent"), { target: { value: "legal-agent" } });
  fireEvent.change(screen.getByLabelText("Node 1 instruction"), { target: { value: "Review clauses" } });
  fireEvent.click(screen.getByLabelText("Auto-routing"));

  fireEvent.click(screen.getByRole("button", { name: "Validate" }));
  await waitFor(() => expect(onValidate).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() =>
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ autoRoutable: true })
    )
  );
});

test("a node key reusing a supervisor phase disables validation and save", () => {
  render(<WorkflowForm agents={[agent("a", "A")]} onValidate={vi.fn()} onSubmit={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Node 1 key"), {
    target: { value: "supervisor_prepare" },
  });

  expect(screen.getByRole("button", { name: "Validate" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
});
