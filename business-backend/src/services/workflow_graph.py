import heapq
from collections.abc import Sequence
from dataclasses import dataclass

from src.schemas.workflow import (
    RESERVED_NODE_KEYS,
    WorkflowEdgeDraft,
    WorkflowLoopBody,
    WorkflowNodeDraft,
)


class WorkflowGraphError(ValueError):
    def __init__(self, code: str, node_keys: Sequence[str] = ()) -> None:
        self.code = code
        self.node_keys = tuple(sorted(node_keys))
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidatedWorkflowGraph:
    topological_order: tuple[str, ...]
    predecessors: dict[str, tuple[str, ...]]
    successors: dict[str, tuple[str, ...]]
    loop_bodies: tuple[WorkflowLoopBody, ...]


def _ancestors(node_key: str, predecessors: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [node_key]
    while stack:
        current = stack.pop()
        for candidate in predecessors[current]:
            if candidate not in seen:
                seen.add(candidate)
                stack.append(candidate)
    return seen


def _descendants(node_key: str, successors: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [node_key]
    while stack:
        current = stack.pop()
        for candidate in successors[current]:
            if candidate not in seen:
                seen.add(candidate)
                stack.append(candidate)
    return seen


def _validate_loop_edges(
    loop_edges: list[WorkflowEdgeDraft],
    predecessors: dict[str, set[str]],
    successors: dict[str, set[str]],
    requires_confirmation: dict[str, bool],
) -> tuple[WorkflowLoopBody, ...]:
    if not loop_edges:
        return ()

    bodies: list[WorkflowLoopBody] = []
    claimed: dict[str, tuple[str, str]] = {}

    for edge in sorted(loop_edges, key=lambda item: (item.source_node_key, item.target_node_key)):
        source = edge.source_node_key
        target = edge.target_node_key

        if source == target:
            body_keys: set[str] = {source}
        else:
            ancestors_of_source = _ancestors(source, predecessors) | {source}
            if target not in ancestors_of_source:
                # No structural path target -> ... -> source exists, so
                # there is no well-defined loop body for this edge to close.
                raise WorkflowGraphError("invalid_loop_edge", [source, target])
            descendants_of_target = _descendants(target, successors) | {target}
            body_keys = descendants_of_target & ancestors_of_source

        overlap = sorted(key for key in body_keys if key in claimed)
        if overlap:
            # v1 scope cut: loop bodies must not nest or share nodes.
            raise WorkflowGraphError("overlapping_loop_bodies", overlap)
        for key in body_keys:
            claimed[key] = (source, target)

        gated = sorted(key for key in body_keys if requires_confirmation.get(key))
        if gated:
            # submit_human_decision (conversation-runner) keys its decision
            # store by node_key only and never clears it — a second loop
            # iteration through a gated node would silently replay the first
            # iteration's decision instead of asking again. Reject at
            # authoring time rather than attempting to make it safe in v1.
            raise WorkflowGraphError("loop_body_requires_confirmation", gated)

        # Only the tail (the loop-back edge's own source) may lead outside
        # the body — any other body-internal node with a structural
        # successor outside the body would run once against iteration 1's
        # output and never re-fire on later iterations, silently freezing on
        # stale input while still reporting "completed".
        non_tail = body_keys - {source}
        fan_out = sorted(key for key in non_tail if successors[key] - body_keys)
        if fan_out:
            raise WorkflowGraphError("loop_body_fan_out", fan_out)

        assert edge.max_iterations is not None  # enforced by WorkflowEdgeDraft
        bodies.append(
            WorkflowLoopBody(
                source_node_key=source,
                target_node_key=target,
                condition=edge.condition,
                max_iterations=edge.max_iterations,
                node_keys=tuple(sorted(body_keys)),
            )
        )
    return tuple(bodies)


def validate_workflow_graph(
    nodes: Sequence[WorkflowNodeDraft],
    edges: Sequence[WorkflowEdgeDraft],
    *,
    max_nodes: int,
) -> ValidatedWorkflowGraph:
    if not nodes:
        raise WorkflowGraphError("empty_graph")
    if len(nodes) > max_nodes:
        raise WorkflowGraphError("too_many_nodes", [node.node_key for node in nodes])

    node_keys = [node.node_key for node in nodes]
    if len(node_keys) != len(set(node_keys)):
        raise WorkflowGraphError("duplicate_node_key", node_keys)

    # Re-checked here, not only in WorkflowNodeDraft, because this validator
    # also runs when an already-stored definition is materialised into a run
    # snapshot — a definition saved before the schema rejected these keys must
    # fail as a clean 409 rather than an IntegrityError 500 on every chat turn.
    reserved = sorted(set(node_keys) & RESERVED_NODE_KEYS)
    if reserved:
        raise WorkflowGraphError("reserved_node_key", reserved)

    known = set(node_keys)
    requires_confirmation = {node.node_key: node.requires_confirmation for node in nodes}
    # Loop-back edges are a deliberate exception to acyclicity and must never
    # enter the structural predecessor/successor graphs below: folding one
    # into `predecessors` the way an ordinary edge is would make the loop's
    # entry node permanently wait on its own tail's outcome, which itself
    # can never arrive without the entry running first — a real deadlock in
    # the AND-join, not just a modelling inconvenience.
    structural_predecessors: dict[str, set[str]] = {key: set() for key in node_keys}
    structural_successors: dict[str, set[str]] = {key: set() for key in node_keys}
    seen_edges: set[tuple[str, str]] = set()
    loop_edges: list[WorkflowEdgeDraft] = []

    for edge in edges:
        source = edge.source_node_key
        target = edge.target_node_key
        unknown = sorted({source, target} - known)
        if unknown:
            raise WorkflowGraphError("dangling_edge", unknown)
        if source == target and not edge.is_loop_back:
            raise WorkflowGraphError("self_edge", [source])
        pair = (source, target)
        if pair in seen_edges:
            raise WorkflowGraphError("duplicate_edge", pair)
        seen_edges.add(pair)
        if edge.is_loop_back:
            loop_edges.append(edge)
            continue
        structural_predecessors[target].add(source)
        structural_successors[source].add(target)

    in_degree = {key: len(structural_predecessors[key]) for key in node_keys}
    ready = [key for key, degree in in_degree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []

    while ready:
        current = heapq.heappop(ready)
        ordered.append(current)
        for target in sorted(structural_successors[current]):
            in_degree[target] -= 1
            if in_degree[target] == 0:
                heapq.heappush(ready, target)

    if len(ordered) != len(node_keys):
        blocked = [key for key, degree in in_degree.items() if degree > 0]
        raise WorkflowGraphError("cycle", blocked)

    loop_bodies = _validate_loop_edges(
        loop_edges, structural_predecessors, structural_successors, requires_confirmation
    )

    return ValidatedWorkflowGraph(
        topological_order=tuple(ordered),
        predecessors={
            key: tuple(sorted(structural_predecessors[key])) for key in sorted(known)
        },
        successors={
            key: tuple(sorted(structural_successors[key])) for key in sorted(known)
        },
        loop_bodies=loop_bodies,
    )
