import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { useState } from "react";

import { WorkflowCanvas } from "@/components/workflows/WorkflowCanvas";
import type {
  AgentDefinition,
  WorkflowEdgeDraft,
  WorkflowNodeDraft,
} from "@/lib/api";

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

const node = (
  overrides: Partial<WorkflowNodeDraft> = {},
): WorkflowNodeDraft => ({
  nodeKey: "alpha",
  displayName: "Alpha",
  agentDefinitionId: "agent-1",
  instruction: "do the thing",
  positionX: 0,
  positionY: 0,
  ...overrides,
});

const agents = [agent("agent-1", "Agent one"), agent("agent-2", "Agent two")];

function StatefulCanvas({
  initialNodes,
  initialEdges = [],
}: {
  initialNodes: WorkflowNodeDraft[];
  initialEdges?: WorkflowEdgeDraft[];
}) {
  const [graph, setGraph] = useState({
    nodes: initialNodes,
    edges: initialEdges,
  });
  return (
    <WorkflowCanvas
      agents={agents}
      nodes={graph.nodes}
      edges={graph.edges}
      onChange={(nodes, edges) => setGraph({ nodes, edges })}
    />
  );
}

test("changing a self-loop condition keeps its iteration settings editable", async () => {
  render(
    <StatefulCanvas
      initialNodes={[node()]}
      initialEdges={[
        {
          sourceNodeKey: "alpha",
          targetNodeKey: "alpha",
          condition: "on_failure",
          isLoopBack: true,
          maxIterations: 7,
        },
      ]}
    />,
  );
  fireEvent.change(
    await screen.findByLabelText("Link condition alpha → alpha"),
    {
      target: { value: "on_success" },
    },
  );
  fireEvent.click(screen.getByLabelText("Loop parameters alpha → alpha"));
  expect(screen.getByLabelText("Loop iterations alpha → alpha")).toHaveValue(7);
});

test("middle deletion followed by adding/editing/deleting keeps the original last node", () => {
  render(
    <StatefulCanvas
      initialNodes={[
        node({ nodeKey: "node_1", displayName: "One" }),
        node({ nodeKey: "node_2", displayName: "Two" }),
        node({ nodeKey: "node_3", displayName: "Three" }),
      ]}
    />,
  );
  fireEvent.click(screen.getByLabelText("Node Two"));
  fireEvent.click(screen.getByRole("button", { name: "Remove node 2" }));
  fireEvent.click(screen.getByRole("button", { name: "Add node" }));
  expect(screen.getByLabelText("Node 3 name")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("Node 3 name"), {
    target: { value: "Four" },
  });
  expect(screen.getByLabelText("Node Three")).toBeInTheDocument();
  expect(screen.getByLabelText("Node Four")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Remove node 3" }));
  expect(screen.getByLabelText("Node Three")).toBeInTheDocument();
  expect(screen.queryByLabelText("Node Four")).not.toBeInTheDocument();
});

test("adding a node appends it and selects its inspector", () => {
  const onChange = vi.fn();
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={[node()]}
      edges={[]}
      onChange={onChange}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "Add node" }));

  expect(onChange).toHaveBeenCalledWith(
    [node(), expect.objectContaining({ nodeKey: "node_2" })],
    [],
  );
});

test("editing the selected node's fields calls onChange with the patch applied", () => {
  const onChange = vi.fn();
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={[node()]}
      edges={[]}
      onChange={onChange}
    />,
  );

  fireEvent.change(screen.getByLabelText("Node 1 name"), {
    target: { value: "Renamed" },
  });

  expect(onChange).toHaveBeenCalledWith(
    [expect.objectContaining({ displayName: "Renamed" })],
    [],
  );
});

test("deleting the selected node removes it and any edge touching it", () => {
  const onChange = vi.fn();
  const nodes = [
    node({ nodeKey: "alpha" }),
    node({ nodeKey: "beta", displayName: "Beta" }),
  ];
  const edges: WorkflowEdgeDraft[] = [
    { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
  ];
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={nodes}
      edges={edges}
      onChange={onChange}
    />,
  );

  fireEvent.click(screen.getByLabelText(`Node ${nodes[0].displayName}`));
  fireEvent.click(screen.getByRole("button", { name: "Remove node 1" }));

  expect(onChange).toHaveBeenCalledWith([nodes[1]], []);
});

test("an existing edge exposes a condition control and a delete affordance", async () => {
  const onChange = vi.fn();
  const nodes = [
    node({ nodeKey: "alpha" }),
    node({ nodeKey: "beta", displayName: "Beta" }),
  ];
  const edges: WorkflowEdgeDraft[] = [
    { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
  ];
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={nodes}
      edges={edges}
      onChange={onChange}
    />,
  );

  fireEvent.change(
    await screen.findByLabelText("Link condition alpha → beta"),
    {
      target: { value: "on_failure" },
    },
  );
  expect(onChange).toHaveBeenCalledWith(nodes, [
    { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_failure" },
  ]);

  fireEvent.click(screen.getByLabelText("Remove link alpha → beta"));
  expect(onChange).toHaveBeenCalledWith(nodes, []);
});

test("connecting a node's source handle to another node's target handle creates an on_success edge", async () => {
  const onChange = vi.fn();
  const nodes = [
    node({ nodeKey: "alpha" }),
    node({ nodeKey: "beta", displayName: "Beta" }),
  ];
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={nodes}
      edges={[]}
      onChange={onChange}
    />,
  );

  fireEvent.click(screen.getByLabelText("Output handle — node Alpha"));
  fireEvent.click(screen.getByLabelText("Input handle — node Beta"));

  expect(onChange).toHaveBeenCalledWith(nodes, [
    { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
  ]);
});

test("a node cannot connect to itself", () => {
  const onChange = vi.fn();
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={[node()]}
      edges={[]}
      onChange={onChange}
    />,
  );

  fireEvent.click(screen.getByLabelText("Output handle — node Alpha"));
  fireEvent.click(screen.getByLabelText("Input handle — node Alpha"));

  expect(onChange).not.toHaveBeenCalled();
});

test("connecting backward to a structural ancestor auto-creates a loop-back edge", async () => {
  const onChange = vi.fn();
  const nodes = [
    node({ nodeKey: "alpha" }),
    node({ nodeKey: "beta", displayName: "Beta" }),
  ];
  const edges: WorkflowEdgeDraft[] = [
    { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
  ];
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={nodes}
      edges={edges}
      onChange={onChange}
    />,
  );

  fireEvent.click(screen.getByLabelText("Output handle — node Beta"));
  fireEvent.click(await screen.findByLabelText("Input handle — node Alpha"));

  expect(onChange).toHaveBeenCalledWith(nodes, [
    ...edges,
    {
      sourceNodeKey: "beta",
      targetNodeKey: "alpha",
      condition: "on_failure",
      isLoopBack: true,
      maxIterations: 3,
    },
  ]);
});

test("a loop edge's badge opens the iterations panel, which patches maxIterations", async () => {
  const onChange = vi.fn();
  const nodes = [
    node({ nodeKey: "alpha" }),
    node({ nodeKey: "beta", displayName: "Beta" }),
  ];
  const edges: WorkflowEdgeDraft[] = [
    {
      sourceNodeKey: "beta",
      targetNodeKey: "alpha",
      condition: "on_failure",
      isLoopBack: true,
      maxIterations: 3,
    },
  ];
  render(
    <WorkflowCanvas
      agents={agents}
      nodes={nodes}
      edges={edges}
      onChange={onChange}
    />,
  );

  expect(
    screen.queryByLabelText("Loop iterations beta → alpha"),
  ).not.toBeInTheDocument();
  fireEvent.click(await screen.findByLabelText("Loop parameters beta → alpha"));

  const input = await screen.findByLabelText("Loop iterations beta → alpha");
  expect(input).toHaveValue(3);
  fireEvent.change(input, { target: { value: "10" } });

  expect(onChange).toHaveBeenCalledWith(nodes, [
    { ...edges[0], maxIterations: 10 },
  ]);
});

test("adding a self-loop from the node inspector hides the button once one exists", () => {
  const onChange = vi.fn();
  const { rerender } = render(
    <WorkflowCanvas
      agents={agents}
      nodes={[node()]}
      edges={[]}
      onChange={onChange}
    />,
  );

  fireEvent.click(
    screen.getByRole("button", { name: "Add a loop on this node 1" }),
  );
  expect(onChange).toHaveBeenCalledWith(
    [node()],
    [
      {
        sourceNodeKey: "alpha",
        targetNodeKey: "alpha",
        condition: "on_failure",
        isLoopBack: true,
        maxIterations: 3,
      },
    ],
  );

  const loopedEdges: WorkflowEdgeDraft[] = [
    {
      sourceNodeKey: "alpha",
      targetNodeKey: "alpha",
      condition: "on_failure",
      isLoopBack: true,
      maxIterations: 3,
    },
  ];
  rerender(
    <WorkflowCanvas
      agents={agents}
      nodes={[node()]}
      edges={loopedEdges}
      onChange={onChange}
    />,
  );
  expect(
    screen.queryByRole("button", { name: "Add a loop on this node 1" }),
  ).not.toBeInTheDocument();
});

test("renaming a node key keeps the inspector input focused", () => {
  render(<StatefulCanvas initialNodes={[node()]} />);
  const input = screen.getByLabelText("Node 1 key");
  input.focus();
  fireEvent.change(input, { target: { value: "bravo" } });
  expect(screen.getByLabelText("Node 1 key")).toHaveFocus();
  expect(screen.getByLabelText("Node 1 key")).toHaveValue("bravo");
});
