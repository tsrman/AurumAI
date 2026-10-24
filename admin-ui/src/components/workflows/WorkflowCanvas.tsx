"use client";

import { useCallback, useMemo, useState } from "react";
import {
  Background,
  Controls,
  EdgeLabelRenderer,
  getBezierPath,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Connection,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type {
  AgentDefinition,
  WorkflowEdgeCondition,
  WorkflowEdgeDraft,
  WorkflowNodeDraft,
} from "@/lib/api";
import {
  addNode,
  addSelfLoop,
  connectNodes,
  hasReservedKey,
  isSelfLoop,
  removeNode,
  setEdgeCondition,
  setLoopMaxIterations,
  toggleEdge,
  updateNode,
} from "@/lib/workflow-graph";
import { useTranslations } from "next-intl";

type Props = {
  agents: AgentDefinition[];
  nodes: WorkflowNodeDraft[];
  edges: WorkflowEdgeDraft[];
  onChange: (nodes: WorkflowNodeDraft[], edges: WorkflowEdgeDraft[]) => void;
};

type NodeCardData = {
  displayName: string;
  nodeKey: string;
  agentName: string;
  requiresConfirmation: boolean;
  selected: boolean;
};

type EdgeData = {
  condition: WorkflowEdgeCondition;
  isLoopBack: boolean;
  onSetCondition: (condition: WorkflowEdgeCondition) => void;
  onRemove: () => void;
  onOpenLoop: () => void;
};

const toFlowNodes = (
  nodes: WorkflowNodeDraft[],
  agents: AgentDefinition[],
  selectedNodeKey: string | null,
): Node<NodeCardData>[] =>
  nodes.map((node) => ({
    id: node.nodeKey,
    type: "workflowNode",
    position: { x: node.positionX ?? 0, y: node.positionY ?? 0 },
    data: {
      displayName: node.displayName || node.nodeKey,
      nodeKey: node.nodeKey,
      agentName:
        agents.find((agent) => agent.id === node.agentDefinitionId)?.name ?? "",
      requiresConfirmation: node.requiresConfirmation ?? false,
      selected: node.nodeKey === selectedNodeKey,
    },
  }));

const toFlowEdges = (
  edges: WorkflowEdgeDraft[],
  onChange: (nodes: WorkflowNodeDraft[], edges: WorkflowEdgeDraft[]) => void,
  nodes: WorkflowNodeDraft[],
  onOpenLoop: (edgeId: string) => void,
): Edge<EdgeData>[] =>
  edges.map((edge) => ({
    id: `${edge.sourceNodeKey}->${edge.targetNodeKey}`,
    type: "workflowEdge",
    source: edge.sourceNodeKey,
    target: edge.targetNodeKey,
    data: {
      condition: edge.condition ?? "on_success",
      isLoopBack: edge.isLoopBack ?? false,
      onSetCondition: (condition) =>
        onChange(
          nodes,
          setEdgeCondition(
            edges,
            edge.sourceNodeKey,
            edge.targetNodeKey,
            condition,
          ),
        ),
      onRemove: () =>
        onChange(
          nodes,
          toggleEdge(
            edges,
            edge.sourceNodeKey,
            edge.targetNodeKey,
            false,
            "on_success",
          ),
        ),
      onOpenLoop: () =>
        onOpenLoop(`${edge.sourceNodeKey}->${edge.targetNodeKey}`),
    },
  }));

function WorkflowNodeCard({ data }: NodeProps<Node<NodeCardData>>) {
  const t = useTranslations("workflows.canvas");
  return (
    <div
      role="group"
      aria-label={t("node", { name: data.displayName || data.nodeKey })}
      className={`w-[180px] rounded-card border bg-aurum-surface px-3 py-3 text-sm text-aurum-text shadow-sm ${
        data.selected
          ? "border-primary-600 ring-2 ring-primary-200"
          : "border-aurum-border"
      }`}
    >
      <Handle
        type="target"
        position={Position.Left}
        aria-label={t("inputHandle", {
          name: data.displayName || data.nodeKey,
        })}
      />
      <p className="truncate font-medium">{data.displayName || data.nodeKey}</p>
      <p className="truncate text-xs text-aurum-text-2">{data.nodeKey}</p>
      {data.agentName ? (
        <p className="truncate text-xs text-aurum-text-2">{data.agentName}</p>
      ) : null}
      {data.requiresConfirmation ? (
        <p className="text-xs text-amber-700">{t("requiresConfirmation")}</p>
      ) : null}
      <Handle
        type="source"
        position={Position.Right}
        aria-label={t("outputHandle", {
          name: data.displayName || data.nodeKey,
        })}
      />
    </div>
  );
}

function WorkflowEdgeCard({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  source,
  target,
  markerEnd,
}: EdgeProps<Edge<EdgeData>>) {
  const t = useTranslations("workflows.canvas");
  const isSelf = source === target;
  let path: string;
  let labelX: number;
  let labelY: number;
  if (isSelf) {
    // getBezierPath's weighted-center label formula lands on top of the
    // node for a self-loop (source/target handles sit on the same node, on
    // opposite sides) — draw a small custom arc above the card instead and
    // anchor the label/badge at its apex, clear of the node.
    const apexY = sourceY - 60;
    path = `M ${sourceX} ${sourceY} C ${sourceX} ${apexY}, ${targetX} ${apexY}, ${targetX} ${targetY}`;
    labelX = (sourceX + targetX) / 2;
    labelY = apexY;
  } else {
    [path, labelX, labelY] = getBezierPath({
      sourceX,
      sourceY,
      targetX,
      targetY,
      sourcePosition,
      targetPosition,
    });
  }
  const condition = data?.condition ?? "on_success";
  const isLoopBack = data?.isLoopBack ?? false;

  return (
    <>
      <path
        id={id}
        className="react-flow__edge-path"
        d={path}
        markerEnd={markerEnd}
        strokeDasharray={isLoopBack ? "4 3" : undefined}
      />
      <EdgeLabelRenderer>
        <div
          style={{
            position: "absolute",
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            pointerEvents: "all",
          }}
          className="flex items-center gap-1 rounded-md border border-aurum-border bg-aurum-surface px-1 py-0.5 text-xs shadow-sm"
        >
          <select
            aria-label={t("edgeCondition", { source, target })}
            className="rounded border-0 bg-transparent text-xs"
            value={condition}
            onChange={(event) =>
              data?.onSetCondition(event.target.value as WorkflowEdgeCondition)
            }
          >
            <option value="on_success">{t("onSuccess")}</option>
            <option value="on_failure">{t("onFailure")}</option>
          </select>
          <button
            type="button"
            aria-label={t("removeEdge", { source, target })}
            className="text-red-700"
            onClick={() => data?.onRemove()}
          >
            ×
          </button>
          {isLoopBack ? (
            <button
              type="button"
              aria-label={t("loopParamsFor", { source, target })}
              title={t("loopParams")}
              onClick={() => data?.onOpenLoop()}
            >
              🔁
            </button>
          ) : null}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const nodeTypes = { workflowNode: WorkflowNodeCard };
const edgeTypes = { workflowEdge: WorkflowEdgeCard };

export function WorkflowCanvas({ agents, nodes, edges, onChange }: Props) {
  const t = useTranslations("workflows.canvas");
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(
    nodes[0]?.nodeKey ?? null,
  );
  // Which loop edge's parameters panel is open, by its "${source}->${target}"
  // id. Deliberately not remapped on rename/delete — every render re-derives
  // the actual edge object below via `find`, so a stale id here just fails
  // to find anything and the panel disappears on its own.
  const [loopEdgeId, setLoopEdgeId] = useState<string | null>(null);

  // Fully controlled: nodes/edges are derived straight from props on every
  // render, so a drag just needs to feed each intermediate position back
  // through onChange — the very next render re-derives flowNodes from the
  // updated draft. No local mirror/effect-sync needed.
  const flowNodes = useMemo(
    () => toFlowNodes(nodes, agents, selectedNodeKey),
    [nodes, agents, selectedNodeKey],
  );
  const flowEdges = useMemo(
    () => toFlowEdges(edges, onChange, nodes, setLoopEdgeId),
    [edges, onChange, nodes],
  );

  const handleNodesChange = useCallback(
    (changes: NodeChange<Node<NodeCardData>>[]) => {
      const positionChanges = changes.filter(
        (
          change,
        ): change is Extract<
          NodeChange<Node<NodeCardData>>,
          { type: "position" }
        > => change.type === "position" && change.position !== undefined,
      );
      if (positionChanges.length === 0) return;
      const nextNodes = nodes.map((node) => {
        const change = positionChanges.find(
          (candidate) => candidate.id === node.nodeKey,
        );
        return change?.position
          ? {
              ...node,
              positionX: change.position.x,
              positionY: change.position.y,
            }
          : node;
      });
      onChange(nextNodes, edges);
    },
    [nodes, edges, onChange],
  );

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      // Self-loops are never created by dragging — react-flow's drag-to-self
      // is unreliable; use the node inspector loop-creation button.
      // instead. A backward drag (target already an ancestor of source) is
      // auto-detected inside connectNodes and becomes a loop-back edge.
      if (
        isSelfLoop({
          sourceNodeKey: connection.source,
          targetNodeKey: connection.target,
        })
      )
        return;
      onChange(
        nodes,
        connectNodes(edges, connection.source, connection.target),
      );
    },
    [nodes, edges, onChange],
  );

  const addWorkflowNode = () => {
    const nextNodes = addNode(nodes);
    onChange(nextNodes, edges);
    setSelectedNodeKey(nextNodes[nextNodes.length - 1].nodeKey);
  };

  const selectedIndex = nodes.findIndex(
    (node) => node.nodeKey === selectedNodeKey,
  );
  const selectedNode = selectedIndex >= 0 ? nodes[selectedIndex] : null;

  const patchSelected = (patch: Partial<WorkflowNodeDraft>) => {
    if (selectedIndex < 0) return;
    const result = updateNode(nodes, edges, selectedIndex, patch);
    onChange(result.nodes, result.edges);
    if (patch.nodeKey !== undefined) setSelectedNodeKey(patch.nodeKey);
  };

  const removeSelected = () => {
    if (!selectedNode) return;
    const result = removeNode(nodes, edges, selectedNode.nodeKey);
    onChange(result.nodes, result.edges);
    setSelectedNodeKey(result.nodes[0]?.nodeKey ?? null);
  };

  // Derived, not remapped: if loopEdgeId no longer matches any edge (deleted,
  // or its id changed because a node it touches was renamed), this is simply
  // undefined and the panel below renders nothing — no cleanup path to keep
  // in sync.
  const selectedLoopEdge = edges.find(
    (edge) =>
      edge.isLoopBack &&
      `${edge.sourceNodeKey}->${edge.targetNodeKey}` === loopEdgeId,
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={addWorkflowNode}
          className="rounded-md border px-3 py-2"
        >
          {t("addNode")}
        </button>
      </div>
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
        <div
          aria-label={t("graph")}
          style={{ height: 460 }}
          className="workflow-editor-canvas min-w-0 rounded-card border border-aurum-border bg-aurum-surface-2"
        >
          <ReactFlowProvider>
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              onNodesChange={handleNodesChange}
              onConnect={handleConnect}
              onNodeClick={(_event, clicked) => setSelectedNodeKey(clicked.id)}
              fitView
            >
              <Background />
              <Controls />
            </ReactFlow>
          </ReactFlowProvider>
        </div>
        <div className="space-y-3">
          {selectedNode ? (
            <fieldset
              key={selectedIndex}
              className="min-w-0 space-y-3 rounded-card border border-aurum-border bg-aurum-surface p-4"
              aria-label={t("inspector", { index: selectedIndex + 1 })}
            >
              <legend className="px-1 font-medium">
                {t("nodeLegend", { index: selectedIndex + 1 })}
              </legend>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">{t("nameLabel")}</span>
                <input
                  aria-label={t("nodeName", { index: selectedIndex + 1 })}
                  placeholder={t("namePlaceholder")}
                  className="w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
                  value={selectedNode.displayName}
                  onChange={(event) =>
                    patchSelected({ displayName: event.target.value })
                  }
                />
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">{t("agentLabel")}</span>
                <select
                  aria-label={t("nodeAgent", { index: selectedIndex + 1 })}
                  className="w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
                  value={selectedNode.agentDefinitionId}
                  onChange={(event) =>
                    patchSelected({ agentDefinitionId: event.target.value })
                  }
                >
                  <option value="">{t("chooseAgent")}</option>
                  {agents.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block space-y-1 text-sm">
                <span className="font-medium">{t("instructionLabel")}</span>
                <textarea
                  aria-label={t("nodeInstruction", {
                    index: selectedIndex + 1,
                  })}
                  className="w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
                  value={selectedNode.instruction}
                  onChange={(event) =>
                    patchSelected({ instruction: event.target.value })
                  }
                />
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  aria-label={t("requiresConfirmationFor", {
                    index: selectedIndex + 1,
                  })}
                  checked={selectedNode.requiresConfirmation ?? false}
                  onChange={(event) =>
                    patchSelected({
                      requiresConfirmation: event.target.checked,
                    })
                  }
                />
                {t("requiresConfirmation")}
              </label>
              <details className="rounded-input border border-aurum-border p-3">
                <summary className="cursor-pointer text-sm">
                  {t("advanced")}
                </summary>
                <div className="mt-3 space-y-3">
                  <label className="block space-y-1 text-sm">
                    <span className="font-medium">{t("keyLabel")}</span>
                    <input
                      aria-label={t("nodeKey", { index: selectedIndex + 1 })}
                      placeholder={t("keyPlaceholder")}
                      className="w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text"
                      value={selectedNode.nodeKey}
                      onChange={(event) =>
                        patchSelected({ nodeKey: event.target.value })
                      }
                    />
                  </label>
                  {hasReservedKey(selectedNode.nodeKey) ? (
                    <p className="text-sm text-red-600">{t("reservedKey")}</p>
                  ) : null}
                  {edges.some(
                    (edge) =>
                      edge.sourceNodeKey === selectedNode.nodeKey &&
                      edge.targetNodeKey === selectedNode.nodeKey,
                  ) ? null : (
                    <button
                      type="button"
                      aria-label={t("addLoopFor", { index: selectedIndex + 1 })}
                      onClick={() =>
                        onChange(
                          nodes,
                          addSelfLoop(edges, selectedNode.nodeKey),
                        )
                      }
                      className="text-sm text-primary-700"
                    >
                      {t("addLoop")}
                    </button>
                  )}
                </div>
              </details>
              {nodes.length > 1 ? (
                <button
                  type="button"
                  onClick={removeSelected}
                  className="text-sm text-red-700"
                >
                  {t("removeNode", { index: selectedIndex + 1 })}
                </button>
              ) : null}
            </fieldset>
          ) : (
            <p className="text-sm text-aurum-text-2">{t("emptyInspector")}</p>
          )}
          {selectedLoopEdge ? (
            <fieldset
              className="min-w-0 space-y-3 rounded-card border border-aurum-border bg-aurum-surface p-4"
              aria-label={t("loopParams")}
            >
              <legend className="px-1 font-medium">
                {t("loopTitle", {
                  source: selectedLoopEdge.sourceNodeKey,
                  target: selectedLoopEdge.targetNodeKey,
                })}
              </legend>
              <label className="block text-sm">
                {t("iterations")}
                <input
                  type="number"
                  min={2}
                  max={20}
                  aria-label={t("loopIterations", {
                    source: selectedLoopEdge.sourceNodeKey,
                    target: selectedLoopEdge.targetNodeKey,
                  })}
                  className="mt-1 w-full rounded-md border px-3 py-2"
                  value={selectedLoopEdge.maxIterations ?? ""}
                  onChange={(event) =>
                    onChange(
                      nodes,
                      setLoopMaxIterations(
                        edges,
                        selectedLoopEdge.sourceNodeKey,
                        selectedLoopEdge.targetNodeKey,
                        event.target.value === ""
                          ? null
                          : Number(event.target.value),
                      ),
                    )
                  }
                />
              </label>
              <p className="text-xs text-aurum-text-2">{t("iterationsHint")}</p>
              <button
                type="button"
                onClick={() => setLoopEdgeId(null)}
                className="text-sm text-aurum-text-2"
              >
                {t("close")}
              </button>
            </fieldset>
          ) : null}
        </div>
      </div>
    </div>
  );
}
