import { describe, expect, it } from "vitest";

import type { WorkflowEdgeDraft, WorkflowNodeDraft } from "@/lib/api";
import {
  addNode,
  addSelfLoop,
  computeAutoLayout,
  computeValidity,
  connectNodes,
  DEFAULT_LOOP_MAX_ITERATIONS,
  emptyNode,
  hasReservedKey,
  isDuplicateKeys,
  isSelfLoop,
  needsAutoLayout,
  removeNode,
  setEdgeCondition,
  setLoopMaxIterations,
  structuralAncestors,
  toggleEdge,
  updateNode,
} from "@/lib/workflow-graph";

const node = (overrides: Partial<WorkflowNodeDraft> = {}): WorkflowNodeDraft => ({
  nodeKey: "alpha",
  displayName: "Alpha",
  agentDefinitionId: "agent-1",
  instruction: "do the thing",
  ...overrides,
});

describe("addNode", () => {
  it("keeps node identities unique after deleting a middle node", () => {
    const initial = [emptyNode(1), emptyNode(2), emptyNode(3)];
    const remaining = removeNode(initial, [], "node_2").nodes;
    const added = addNode(remaining);
    expect(new Set(added.map(n => n.nodeKey)).size).toBe(3);
    expect(removeNode(added, [], added[2].nodeKey).nodes).toEqual(remaining);
  });
  it("appends an empty node keyed by the next index", () => {
    const nodes = addNode([node()]);
    expect(nodes).toHaveLength(2);
    expect(nodes[1]).toEqual(emptyNode(2));
  });
});

describe("loop condition regression", () => {
  it.each(["alpha", "beta"])("preserves loop metadata targeting %s", (target) => {
    const edge: WorkflowEdgeDraft = { sourceNodeKey: "alpha", targetNodeKey: target,
      condition: "on_failure", isLoopBack: true, maxIterations: 7 };
    expect(setEdgeCondition([edge], "alpha", target, "on_success"))
      .toEqual([{ ...edge, condition: "on_success" }]);
    expect(edge.condition).toBe("on_failure");
  });
});

describe("removeNode", () => {
  it("drops the node and any edge touching it", () => {
    const nodes = [node({ nodeKey: "alpha" }), node({ nodeKey: "beta" })];
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    const result = removeNode(nodes, edges, "alpha");
    expect(result.nodes.map((n) => n.nodeKey)).toEqual(["beta"]);
    expect(result.edges).toEqual([]);
  });

  it("leaves unrelated edges untouched", () => {
    const nodes = [node({ nodeKey: "alpha" }), node({ nodeKey: "beta" }), node({ nodeKey: "gamma" })];
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "beta", targetNodeKey: "gamma", condition: "on_success" },
    ];
    const result = removeNode(nodes, edges, "alpha");
    expect(result.edges).toEqual(edges);
  });
});

describe("updateNode", () => {
  it("patches fields in place without touching edges when the key is unchanged", () => {
    const nodes = [node()];
    const edges: WorkflowEdgeDraft[] = [];
    const result = updateNode(nodes, edges, 0, { displayName: "Renamed" });
    expect(result.nodes[0].displayName).toBe("Renamed");
    expect(result.edges).toBe(edges);
  });

  it("propagates a node-key rename into every referencing edge", () => {
    const nodes = [node({ nodeKey: "alpha" }), node({ nodeKey: "beta" })];
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
      { sourceNodeKey: "beta", targetNodeKey: "alpha", condition: "on_failure" },
    ];
    const result = updateNode(nodes, edges, 0, { nodeKey: "renamed" });
    expect(result.edges).toEqual([
      { sourceNodeKey: "renamed", targetNodeKey: "beta", condition: "on_success" },
      { sourceNodeKey: "beta", targetNodeKey: "renamed", condition: "on_failure" },
    ]);
  });
});

describe("toggleEdge / setEdgeCondition", () => {
  it("adds an edge when checked", () => {
    const edges = toggleEdge([], "alpha", "beta", true, "on_success");
    expect(edges).toEqual([{ sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" }]);
  });

  it("removes the matching edge when unchecked", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    expect(toggleEdge(edges, "alpha", "beta", false, "on_success")).toEqual([]);
  });

  it("setEdgeCondition replaces the condition on the same pair", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    expect(setEdgeCondition(edges, "alpha", "beta", "on_failure")).toEqual([
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_failure" },
    ]);
  });
});

describe("isSelfLoop / isDuplicateKeys / hasReservedKey", () => {
  it("flags a self-referencing edge", () => {
    expect(isSelfLoop({ sourceNodeKey: "alpha", targetNodeKey: "alpha" })).toBe(true);
    expect(isSelfLoop({ sourceNodeKey: "alpha", targetNodeKey: "beta" })).toBe(false);
  });

  it("flags duplicate node keys", () => {
    expect(isDuplicateKeys([node({ nodeKey: "alpha" }), node({ nodeKey: "alpha" })])).toBe(true);
    expect(isDuplicateKeys([node({ nodeKey: "alpha" }), node({ nodeKey: "beta" })])).toBe(false);
  });

  it("flags reserved supervisor phase keys", () => {
    expect(hasReservedKey("supervisor_prepare")).toBe(true);
    expect(hasReservedKey("supervisor_synthesize")).toBe(true);
    expect(hasReservedKey("alpha")).toBe(false);
  });
});

describe("structuralAncestors", () => {
  it("finds a direct predecessor", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    expect(structuralAncestors(edges, "beta")).toEqual(new Set(["alpha"]));
  });

  it("finds a transitive ancestor through a chain", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
      { sourceNodeKey: "beta", targetNodeKey: "gamma", condition: "on_success" },
    ];
    expect(structuralAncestors(edges, "gamma")).toEqual(new Set(["alpha", "beta"]));
  });

  it("ignores an existing loop-back edge when computing ancestors", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
      { sourceNodeKey: "beta", targetNodeKey: "alpha", condition: "on_failure", isLoopBack: true, maxIterations: 3 },
    ];
    // Without excluding the loop edge this would (wrongly) find alpha as its
    // own ancestor via the cycle it deliberately closes.
    expect(structuralAncestors(edges, "alpha")).toEqual(new Set());
  });

  it("returns an empty set for an unrelated or leaf node", () => {
    expect(structuralAncestors([], "alpha")).toEqual(new Set());
  });
});

describe("connectNodes", () => {
  it("creates an ordinary on_success edge for an unrelated forward connection", () => {
    expect(connectNodes([], "alpha", "beta")).toEqual([
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ]);
  });

  it("auto-detects a direct backward connection as a loop-back edge", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    expect(connectNodes(edges, "beta", "alpha")).toEqual([
      ...edges,
      {
        sourceNodeKey: "beta",
        targetNodeKey: "alpha",
        condition: "on_failure",
        isLoopBack: true,
        maxIterations: DEFAULT_LOOP_MAX_ITERATIONS,
      },
    ]);
  });

  it("auto-detects a transitive backward connection as a loop-back edge", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
      { sourceNodeKey: "beta", targetNodeKey: "gamma", condition: "on_success" },
    ];
    const result = connectNodes(edges, "gamma", "alpha");
    expect(result[2]).toEqual({
      sourceNodeKey: "gamma",
      targetNodeKey: "alpha",
      condition: "on_failure",
      isLoopBack: true,
      maxIterations: DEFAULT_LOOP_MAX_ITERATIONS,
    });
  });

  it("does not treat an unrelated node as backward", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    expect(connectNodes(edges, "alpha", "gamma")).toEqual([
      ...edges,
      { sourceNodeKey: "alpha", targetNodeKey: "gamma", condition: "on_success" },
    ]);
  });

  it("replaces rather than duplicates a reconnection of the same pair", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    expect(connectNodes(edges, "alpha", "beta")).toEqual([
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ]);
  });

  it("is a no-op for a self connection", () => {
    const edges: WorkflowEdgeDraft[] = [];
    expect(connectNodes(edges, "alpha", "alpha")).toBe(edges);
  });
});

describe("addSelfLoop", () => {
  it("adds a self-loop with default condition and iterations", () => {
    expect(addSelfLoop([], "alpha")).toEqual([
      {
        sourceNodeKey: "alpha",
        targetNodeKey: "alpha",
        condition: "on_failure",
        isLoopBack: true,
        maxIterations: DEFAULT_LOOP_MAX_ITERATIONS,
      },
    ]);
  });

  it("is a no-op if the node already has a self-loop", () => {
    const edges: WorkflowEdgeDraft[] = [
      {
        sourceNodeKey: "alpha",
        targetNodeKey: "alpha",
        condition: "on_failure",
        isLoopBack: true,
        maxIterations: 5,
      },
    ];
    expect(addSelfLoop(edges, "alpha")).toBe(edges);
  });
});

describe("setLoopMaxIterations", () => {
  it("patches only the matching edge", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "alpha", condition: "on_failure", isLoopBack: true, maxIterations: 3 },
      { sourceNodeKey: "beta", targetNodeKey: "gamma", condition: "on_success" },
    ];
    const result = setLoopMaxIterations(edges, "alpha", "alpha", 10);
    expect(result[0].maxIterations).toBe(10);
    expect(result[1]).toBe(edges[1]);
  });

  it("accepts null for a cleared input", () => {
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "alpha", condition: "on_failure", isLoopBack: true, maxIterations: 3 },
    ];
    expect(setLoopMaxIterations(edges, "alpha", "alpha", null)[0].maxIterations).toBeNull();
  });
});

describe("computeValidity", () => {
  const validDraft = {
    name: "Workflow",
    supervisorAgentDefinitionId: "sup-1",
    nodes: [node()],
    edges: [] as WorkflowEdgeDraft[],
  };

  it("is valid for a well-formed draft", () => {
    expect(computeValidity(validDraft)).toBe(false);
  });

  it("is invalid when name is blank", () => {
    expect(computeValidity({ ...validDraft, name: "  " })).toBe(true);
  });

  it("is invalid without a supervisor", () => {
    expect(computeValidity({ ...validDraft, supervisorAgentDefinitionId: "" })).toBe(true);
  });

  it("is invalid on duplicate node keys", () => {
    expect(
      computeValidity({ ...validDraft, nodes: [node(), node()] })
    ).toBe(true);
  });

  it("is invalid on a reserved node key", () => {
    expect(
      computeValidity({ ...validDraft, nodes: [node({ nodeKey: "supervisor_prepare" })] })
    ).toBe(true);
  });

  it("is invalid on a malformed node key", () => {
    expect(
      computeValidity({ ...validDraft, nodes: [node({ nodeKey: "Alpha!" })] })
    ).toBe(true);
  });

  it("is invalid when a node is missing required text", () => {
    expect(
      computeValidity({ ...validDraft, nodes: [node({ instruction: "  " })] })
    ).toBe(true);
    expect(
      computeValidity({ ...validDraft, nodes: [node({ displayName: "" })] })
    ).toBe(true);
    expect(
      computeValidity({ ...validDraft, nodes: [node({ agentDefinitionId: "" })] })
    ).toBe(true);
  });

  it("is invalid on an ordinary (non-loop) self-loop edge", () => {
    expect(
      computeValidity({
        ...validDraft,
        nodes: [node({ nodeKey: "alpha" }), node({ nodeKey: "beta" })],
        edges: [{ sourceNodeKey: "alpha", targetNodeKey: "alpha", condition: "on_success" }],
      })
    ).toBe(true);
  });

  it("is valid for a self-loop marked isLoopBack with in-range iterations", () => {
    expect(
      computeValidity({
        ...validDraft,
        edges: [
          {
            sourceNodeKey: "alpha",
            targetNodeKey: "alpha",
            condition: "on_failure",
            isLoopBack: true,
            maxIterations: 3,
          },
        ],
      })
    ).toBe(false);
  });

  it("is invalid when a loop edge's maxIterations is missing or out of range", () => {
    const loopEdge = (maxIterations: number | null): WorkflowEdgeDraft => ({
      sourceNodeKey: "alpha",
      targetNodeKey: "alpha",
      condition: "on_failure",
      isLoopBack: true,
      maxIterations,
    });
    expect(computeValidity({ ...validDraft, edges: [loopEdge(null)] })).toBe(true);
    expect(computeValidity({ ...validDraft, edges: [loopEdge(1)] })).toBe(true);
    expect(computeValidity({ ...validDraft, edges: [loopEdge(21)] })).toBe(true);
  });
});

describe("needsAutoLayout", () => {
  it("is true when any node has a null position", () => {
    expect(needsAutoLayout([node({ positionX: null, positionY: null })])).toBe(true);
    expect(needsAutoLayout([node()])).toBe(true); // positions undefined by default
  });

  it("is false once every node has real coordinates", () => {
    expect(needsAutoLayout([node({ positionX: 0, positionY: 0 })])).toBe(false);
  });
});

describe("computeAutoLayout", () => {
  it("places independent nodes in the same column at different rows", () => {
    const nodes = [node({ nodeKey: "alpha" }), node({ nodeKey: "beta" })];
    const laidOut = computeAutoLayout(nodes, []);
    expect(laidOut[0].positionX).toBe(0);
    expect(laidOut[1].positionX).toBe(0);
    expect(laidOut[0].positionY).not.toBe(laidOut[1].positionY);
  });

  it("places a dependent node one column after its predecessor", () => {
    const nodes = [node({ nodeKey: "alpha" }), node({ nodeKey: "beta" })];
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
    ];
    const laidOut = computeAutoLayout(nodes, edges);
    const byKey = Object.fromEntries(laidOut.map((n) => [n.nodeKey, n]));
    expect(byKey.beta.positionX).toBeGreaterThan(byKey.alpha.positionX!);
  });

  it("uses the longest path depth for an AND-join node", () => {
    const nodes = [
      node({ nodeKey: "alpha" }),
      node({ nodeKey: "beta" }),
      node({ nodeKey: "gamma" }),
      node({ nodeKey: "join" }),
    ];
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
      { sourceNodeKey: "beta", targetNodeKey: "join", condition: "on_success" },
      { sourceNodeKey: "gamma", targetNodeKey: "join", condition: "on_success" },
    ];
    const laidOut = computeAutoLayout(nodes, edges);
    const byKey = Object.fromEntries(laidOut.map((n) => [n.nodeKey, n]));
    // join depends on beta (depth 1) and gamma (depth 0) directly — longest path wins.
    expect(byKey.join.positionX).toBe(byKey.beta.positionX! + 280);
  });

  it("does not loop forever on a cyclic edge set", () => {
    const nodes = [node({ nodeKey: "alpha" }), node({ nodeKey: "beta" })];
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "alpha", targetNodeKey: "beta", condition: "on_success" },
      { sourceNodeKey: "beta", targetNodeKey: "alpha", condition: "on_success" },
    ];
    expect(() => computeAutoLayout(nodes, edges)).not.toThrow();
  });

  it("excludes a loop-back edge from depth computation, laying out the forward chain normally", () => {
    // a -> b -> c, plus a loop-back c -> a. Without excluding the loop edge,
    // the structural graph looks cyclic and depthOf's cycle-guard bakes a
    // truncated depth into an ancestor's memoized value.
    const nodes = [node({ nodeKey: "a" }), node({ nodeKey: "b" }), node({ nodeKey: "c" })];
    const edges: WorkflowEdgeDraft[] = [
      { sourceNodeKey: "a", targetNodeKey: "b", condition: "on_success" },
      { sourceNodeKey: "b", targetNodeKey: "c", condition: "on_success" },
      { sourceNodeKey: "c", targetNodeKey: "a", condition: "on_failure", isLoopBack: true, maxIterations: 3 },
    ];
    const laidOut = computeAutoLayout(nodes, edges);
    const byKey = Object.fromEntries(laidOut.map((n) => [n.nodeKey, n]));
    expect(byKey.a.positionX).toBe(0);
    expect(byKey.b.positionX).toBe(280);
    expect(byKey.c.positionX).toBe(560);
  });
});
