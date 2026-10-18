import type {
  WorkflowEdgeCondition,
  WorkflowEdgeDraft,
  WorkflowNodeDraft,
} from "@/lib/api";

// Mirrors RESERVED_NODE_KEYS in business-backend's workflow schema: these two
// keys name the supervisor phases in workflow_node_runs, so a node reusing one
// makes every run of the definition fail. Backend validation is authoritative;
// this only surfaces it before the round trip.
export const RESERVED_NODE_KEYS = new Set(["supervisor_prepare", "supervisor_synthesize"]);

const NODE_KEY_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;

// Wider than the node card's fixed 160px width (WorkflowCanvas.tsx) with a
// comfortable margin — cards at this spacing never overlap enough for one
// node's DOM to intercept pointer events meant for its neighbor's handle.
const AUTO_LAYOUT_COLUMN_WIDTH = 280;
const AUTO_LAYOUT_ROW_HEIGHT = 140;

export function emptyNode(index: number): WorkflowNodeDraft {
  return {
    nodeKey: `node_${index}`,
    displayName: "",
    agentDefinitionId: "",
    instruction: "",
    // Cascade new nodes so they don't spawn stacked exactly on top of each
    // other at (0,0) — indistinguishable on the canvas and, worse, only the
    // topmost one would ever receive a click or drag.
    positionX: (index - 1) * AUTO_LAYOUT_COLUMN_WIDTH,
    positionY: 0,
  };
}

export function addNode(nodes: WorkflowNodeDraft[]): WorkflowNodeDraft[] {
  const keys = new Set(nodes.map(node => node.nodeKey));
  let index = nodes.length + 1;
  while (keys.has(`node_${index}`)) index += 1;
  return [...nodes, emptyNode(index)];
}

export function removeNode(
  nodes: WorkflowNodeDraft[],
  edges: WorkflowEdgeDraft[],
  nodeKey: string
): { nodes: WorkflowNodeDraft[]; edges: WorkflowEdgeDraft[] } {
  return {
    nodes: nodes.filter((node) => node.nodeKey !== nodeKey),
    edges: edges.filter(
      (edge) => edge.sourceNodeKey !== nodeKey && edge.targetNodeKey !== nodeKey
    ),
  };
}

export function updateNode(
  nodes: WorkflowNodeDraft[],
  edges: WorkflowEdgeDraft[],
  index: number,
  patch: Partial<WorkflowNodeDraft>
): { nodes: WorkflowNodeDraft[]; edges: WorkflowEdgeDraft[] } {
  const oldKey = nodes[index].nodeKey;
  const nextNodes = nodes.map((node, position) =>
    position === index ? { ...node, ...patch } : node
  );
  const newKey = nextNodes[index].nodeKey;
  if (newKey === oldKey) {
    return { nodes: nextNodes, edges };
  }
  const nextEdges = edges.map((edge) => ({
    ...edge,
    sourceNodeKey: edge.sourceNodeKey === oldKey ? newKey : edge.sourceNodeKey,
    targetNodeKey: edge.targetNodeKey === oldKey ? newKey : edge.targetNodeKey,
  }));
  return { nodes: nextNodes, edges: nextEdges };
}

export function toggleEdge(
  edges: WorkflowEdgeDraft[],
  sourceNodeKey: string,
  targetNodeKey: string,
  checked: boolean,
  condition: WorkflowEdgeCondition
): WorkflowEdgeDraft[] {
  const withoutPair = edges.filter(
    (edge) =>
      !(edge.targetNodeKey === targetNodeKey && edge.sourceNodeKey === sourceNodeKey)
  );
  return checked
    ? [...withoutPair, { sourceNodeKey, targetNodeKey, condition }]
    : withoutPair;
}

export function setEdgeCondition(
  edges: WorkflowEdgeDraft[],
  sourceNodeKey: string,
  targetNodeKey: string,
  condition: WorkflowEdgeCondition
): WorkflowEdgeDraft[] {
  return edges.map(edge =>
    edge.sourceNodeKey === sourceNodeKey && edge.targetNodeKey === targetNodeKey
      ? { ...edge, condition }
      : edge
  );
}

export function isSelfLoop(edge: Pick<WorkflowEdgeDraft, "sourceNodeKey" | "targetNodeKey">): boolean {
  return edge.sourceNodeKey === edge.targetNodeKey;
}

// Mirrors business-backend's 2..20 CHECK constraint on max_iterations.
export const DEFAULT_LOOP_MAX_ITERATIONS = 3;

// Structural ancestors of nodeKey over edges WITHOUT isLoopBack — mirrors
// workflow_graph.py's _ancestors(). A loop-back edge is the one deliberate
// exception to the DAG's acyclic-only rule, so it must never contribute to
// this traversal: folding it in would make an ancestor search over a graph
// that already contains a cycle, which is exactly what this function exists
// to detect in the first place.
export function structuralAncestors(edges: WorkflowEdgeDraft[], nodeKey: string): Set<string> {
  const predecessors = new Map<string, string[]>();
  for (const edge of edges) {
    if (edge.isLoopBack) continue;
    const list = predecessors.get(edge.targetNodeKey) ?? [];
    list.push(edge.sourceNodeKey);
    predecessors.set(edge.targetNodeKey, list);
  }
  const seen = new Set<string>();
  const stack = [nodeKey];
  while (stack.length > 0) {
    const current = stack.pop()!;
    for (const candidate of predecessors.get(current) ?? []) {
      if (!seen.has(candidate)) {
        seen.add(candidate);
        stack.push(candidate);
      }
    }
  }
  return seen;
}

// The canvas gesture for creating a loop: dragging a connection whose drop
// target is already a structural ancestor of the drag source auto-creates a
// loop-back edge (source stays the "tail" the backend's loop-body traversal
// expects) instead of an ordinary forward edge — no separate toggle. A
// self-loop (source === target) is intentionally excluded here; it isn't
// reliably drag-able in react-flow and is created via addSelfLoop instead.
export function connectNodes(
  edges: WorkflowEdgeDraft[],
  sourceNodeKey: string,
  targetNodeKey: string
): WorkflowEdgeDraft[] {
  if (sourceNodeKey === targetNodeKey) return edges;
  const backward = structuralAncestors(edges, sourceNodeKey).has(targetNodeKey);
  const newEdge: WorkflowEdgeDraft = backward
    ? {
        sourceNodeKey,
        targetNodeKey,
        // on_failure: conversation-runner's _should_loop_back re-triggers the
        // loop on the outcome named by `condition` — "retry until success or
        // exhaust max_iterations" is the more common loop shape than
        // "iterate while succeeding".
        condition: "on_failure",
        isLoopBack: true,
        maxIterations: DEFAULT_LOOP_MAX_ITERATIONS,
      }
    : { sourceNodeKey, targetNodeKey, condition: "on_success" };
  const withoutPair = edges.filter(
    (edge) => !(edge.sourceNodeKey === sourceNodeKey && edge.targetNodeKey === targetNodeKey)
  );
  return [...withoutPair, newEdge];
}

export function addSelfLoop(edges: WorkflowEdgeDraft[], nodeKey: string): WorkflowEdgeDraft[] {
  const alreadyLooped = edges.some(
    (edge) => edge.sourceNodeKey === nodeKey && edge.targetNodeKey === nodeKey
  );
  if (alreadyLooped) return edges; // backend forbids a duplicate (source, target) pair
  return [
    ...edges,
    {
      sourceNodeKey: nodeKey,
      targetNodeKey: nodeKey,
      condition: "on_failure",
      isLoopBack: true,
      maxIterations: DEFAULT_LOOP_MAX_ITERATIONS,
    },
  ];
}

export function setLoopMaxIterations(
  edges: WorkflowEdgeDraft[],
  sourceNodeKey: string,
  targetNodeKey: string,
  maxIterations: number | null
): WorkflowEdgeDraft[] {
  return edges.map((edge) =>
    edge.sourceNodeKey === sourceNodeKey && edge.targetNodeKey === targetNodeKey
      ? { ...edge, maxIterations }
      : edge
  );
}

export function isDuplicateKeys(nodes: WorkflowNodeDraft[]): boolean {
  const keys = nodes.map((node) => node.nodeKey);
  return new Set(keys).size !== keys.length;
}

export function hasReservedKey(nodeKey: string): boolean {
  return RESERVED_NODE_KEYS.has(nodeKey);
}

export function computeValidity(draft: {
  name: string;
  supervisorAgentDefinitionId: string;
  nodes: WorkflowNodeDraft[];
  edges: WorkflowEdgeDraft[];
}): boolean {
  return (
    !draft.name.trim() ||
    !draft.supervisorAgentDefinitionId ||
    isDuplicateKeys(draft.nodes) ||
    draft.nodes.some(
      (node) =>
        !NODE_KEY_PATTERN.test(node.nodeKey) ||
        hasReservedKey(node.nodeKey) ||
        !node.displayName.trim() ||
        !node.agentDefinitionId ||
        !node.instruction.trim()
    ) ||
    // An ordinary self-edge is still invalid (backend's self_edge check); a
    // self-loop marked isLoopBack is the one deliberate exception.
    draft.edges.some((edge) => isSelfLoop(edge) && !edge.isLoopBack) ||
    draft.edges.some(
      (edge) =>
        edge.isLoopBack &&
        (edge.maxIterations == null || edge.maxIterations < 2 || edge.maxIterations > 20)
    )
  );
}

// Deterministic layered-grid fallback for definitions authored before this
// feature (or via a template — canvas positions were out of scope for
// 2026-09-05-workflow-templates-design.md): column = topological depth
// (longest path from a root), row = index within that column. Not a real
// layout algorithm — the graph is capped at 20 nodes, so a dagre-style
// solver isn't warranted.
export function computeAutoLayout(
  nodes: WorkflowNodeDraft[],
  edges: WorkflowEdgeDraft[]
): WorkflowNodeDraft[] {
  // Loop-back edges are excluded: they're the one deliberate exception to
  // the DAG's acyclic-only rule, so including one here would feed depthOf a
  // graph that genuinely contains a cycle, corrupting the memoized depth of
  // whichever ancestor the cyclic-guard below truncates through.
  const predecessorsByKey = new Map<string, string[]>();
  for (const node of nodes) predecessorsByKey.set(node.nodeKey, []);
  for (const edge of edges) {
    if (edge.isLoopBack) continue;
    predecessorsByKey.get(edge.targetNodeKey)?.push(edge.sourceNodeKey);
  }

  const depthByKey = new Map<string, number>();
  const depthOf = (nodeKey: string, seen: Set<string>): number => {
    if (depthByKey.has(nodeKey)) return depthByKey.get(nodeKey)!;
    if (seen.has(nodeKey)) return 0; // cyclic input — draw it at column 0 rather than looping
    seen.add(nodeKey);
    const predecessors = predecessorsByKey.get(nodeKey) ?? [];
    const depth =
      predecessors.length === 0
        ? 0
        : 1 + Math.max(...predecessors.map((key) => depthOf(key, seen)));
    depthByKey.set(nodeKey, depth);
    return depth;
  };
  for (const node of nodes) depthOf(node.nodeKey, new Set());

  const rowByDepth = new Map<number, number>();
  return nodes.map((node) => {
    const depth = depthByKey.get(node.nodeKey) ?? 0;
    const row = rowByDepth.get(depth) ?? 0;
    rowByDepth.set(depth, row + 1);
    return {
      ...node,
      positionX: depth * AUTO_LAYOUT_COLUMN_WIDTH,
      positionY: row * AUTO_LAYOUT_ROW_HEIGHT,
    };
  });
}

export function needsAutoLayout(nodes: WorkflowNodeDraft[]): boolean {
  return nodes.some((node) => node.positionX == null || node.positionY == null);
}
