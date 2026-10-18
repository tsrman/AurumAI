from collections.abc import Sequence
from uuid import uuid4

import pytest

from src.schemas.workflow import WorkflowEdgeDraft, WorkflowNodeDraft
from src.services.workflow_graph import WorkflowGraphError, validate_workflow_graph


def node(key: str, *, requires_confirmation: bool = False) -> WorkflowNodeDraft:
    return WorkflowNodeDraft(
        nodeKey=key,
        displayName=key.upper(),
        agentDefinitionId=uuid4(),
        instruction=f"Run {key}",
        requiresConfirmation=requires_confirmation,
    )


def edge(source: str, target: str, *, condition: str = "on_success") -> WorkflowEdgeDraft:
    return WorkflowEdgeDraft(
        sourceNodeKey=source, targetNodeKey=target, condition=condition
    )


def loop_edge(
    source: str,
    target: str,
    *,
    condition: str = "on_failure",
    max_iterations: int = 3,
) -> WorkflowEdgeDraft:
    return WorkflowEdgeDraft(
        sourceNodeKey=source,
        targetNodeKey=target,
        condition=condition,
        isLoopBack=True,
        maxIterations=max_iterations,
    )


@pytest.mark.parametrize(
    ("nodes", "edges", "code"),
    [
        ([], [], "empty_graph"),
        ([node("a")], [edge("a", "a")], "self_edge"),
        ([node("a"), node("b")], [edge("a", "b"), edge("b", "a")], "cycle"),
        ([node("a")], [edge("a", "missing")], "dangling_edge"),
        ([node("a"), node("a")], [], "duplicate_node_key"),
        (
            [node("a"), node("b")],
            [edge("a", "b"), edge("a", "b")],
            "duplicate_edge",
        ),
    ],
)
def test_invalid_graphs_raise_stable_codes(
    nodes: Sequence[WorkflowNodeDraft],
    edges: Sequence[WorkflowEdgeDraft],
    code: str,
) -> None:
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(nodes, edges, max_nodes=20)

    assert caught.value.code == code


def test_duplicate_edge_detection_ignores_condition() -> None:
    # Uniqueness is (source, target) only — an on_success and an on_failure
    # edge between the same pair is still "duplicate_edge", not two distinct
    # edges. Under AND-join, allowing both would make the target permanently
    # unreachable (no single outcome of the source satisfies both), so this
    # is a deliberate design choice, not an oversight.
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(
            [node("a"), node("b")],
            [
                edge("a", "b"),
                WorkflowEdgeDraft(
                    sourceNodeKey="a", targetNodeKey="b", condition="on_failure"
                ),
            ],
            max_nodes=20,
        )

    assert caught.value.code == "duplicate_edge"


def test_graph_above_operator_ceiling_is_rejected() -> None:
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph([node("a"), node("b")], [], max_nodes=1)

    assert caught.value.code == "too_many_nodes"
    assert caught.value.node_keys == ("a", "b")


def test_multiple_roots_and_disconnected_components_have_stable_lexical_order() -> None:
    nodes = [node("z"), node("b"), node("a"), node("c")]
    edges = [edge("b", "c")]

    first = validate_workflow_graph(nodes, edges, max_nodes=20)
    second = validate_workflow_graph(
        list(reversed(nodes)), list(reversed(edges)), max_nodes=20
    )

    assert first.topological_order == ("a", "b", "c", "z")
    assert second.topological_order == first.topological_order
    assert first.predecessors == {
        "a": (),
        "b": (),
        "c": ("b",),
        "z": (),
    }
    assert first.successors == {
        "a": (),
        "b": ("c",),
        "c": (),
        "z": (),
    }


def test_cycle_error_identifies_only_nodes_still_blocked() -> None:
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(
            [node("a"), node("b"), node("c")],
            [edge("a", "b"), edge("b", "a")],
            max_nodes=20,
        )

    assert caught.value.code == "cycle"
    assert caught.value.node_keys == ("a", "b")


def test_reserved_supervisor_phase_keys_are_rejected() -> None:
    """workflow_node_runs keys the two supervisor phases and every task node in
    one table, so a node reusing a phase key violates
    uq_workflow_node_runs_run_phase at run creation — a 500 on every chat turn
    of an otherwise valid definition."""
    with pytest.raises(WorkflowGraphError) as error:
        validate_workflow_graph(
            [
                # model_construct: the schema now rejects this key outright, so
                # only a definition stored before that rule can carry it — which
                # is exactly the case this graph check has to catch.
                WorkflowNodeDraft.model_construct(
                    node_key="supervisor_prepare",
                    display_name="SUPERVISOR_PREPARE",
                    agent_definition_id=uuid4(),
                    instruction="Run supervisor_prepare",
                ),
                node("research"),
            ],
            [],
            max_nodes=20,
        )

    assert error.value.code == "reserved_node_key"
    assert error.value.node_keys == ("supervisor_prepare",)


def test_self_loop_back_edge_is_valid_and_reports_a_single_node_body() -> None:
    graph = validate_workflow_graph(
        [node("check")], [loop_edge("check", "check")], max_nodes=20
    )

    assert len(graph.loop_bodies) == 1
    body = graph.loop_bodies[0]
    assert body.source_node_key == "check"
    assert body.target_node_key == "check"
    assert body.condition == "on_failure"
    assert body.max_iterations == 3
    assert body.node_keys == ("check",)


def test_ancestor_based_loop_back_edge_reports_the_full_body() -> None:
    # start -> middle -> check, with a loop-back from check to start: the
    # body is every node on that structural path, inclusive.
    graph = validate_workflow_graph(
        [node("start"), node("middle"), node("check")],
        [edge("start", "middle"), edge("middle", "check"), loop_edge("check", "start")],
        max_nodes=20,
    )

    assert len(graph.loop_bodies) == 1
    body = graph.loop_bodies[0]
    assert body.node_keys == ("check", "middle", "start")


def test_loop_back_edge_to_a_non_ancestor_is_rejected() -> None:
    # a -> b structural; a loop-back from b to c has no structural path
    # c -> ... -> b, so there is no well-defined body for it to close.
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(
            [node("a"), node("b"), node("c")],
            [edge("a", "b"), loop_edge("b", "c")],
            max_nodes=20,
        )

    assert caught.value.code == "invalid_loop_edge"


def test_overlapping_loop_bodies_are_rejected() -> None:
    # Two self-loops on the same node inevitably overlap; more realistically
    # this also catches two distinct loop-back edges sharing a body node.
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(
            [node("start"), node("check")],
            [
                edge("start", "check"),
                loop_edge("check", "start"),
                loop_edge("check", "check", condition="on_success"),
            ],
            max_nodes=20,
        )

    assert caught.value.code == "overlapping_loop_bodies"
    assert "check" in caught.value.node_keys


def test_requires_confirmation_inside_a_loop_body_is_rejected() -> None:
    # submit_human_decision (conversation-runner) keys its stored decision by
    # node_key only and never clears it — a second loop iteration through a
    # gated node would silently replay the first iteration's decision.
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(
            [node("start", requires_confirmation=True), node("check")],
            [edge("start", "check"), loop_edge("check", "start")],
            max_nodes=20,
        )

    assert caught.value.code == "loop_body_requires_confirmation"
    assert caught.value.node_keys == ("start",)


def test_fan_out_from_a_non_tail_body_node_is_rejected() -> None:
    # "middle" is inside the check->start loop body but also feeds "outside"
    # directly — outside would run once against iteration 1's output and
    # never re-fire on later iterations.
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(
            [node("start"), node("middle"), node("check"), node("outside")],
            [
                edge("start", "middle"),
                edge("middle", "check"),
                edge("middle", "outside"),
                loop_edge("check", "start"),
            ],
            max_nodes=20,
        )

    assert caught.value.code == "loop_body_fan_out"
    assert caught.value.node_keys == ("middle",)


def test_tail_fan_out_is_allowed_as_the_loops_exit_path() -> None:
    # The tail's own non-loop successor IS the loop's normal exit edge —
    # explicitly exempt from the fan-out rule.
    graph = validate_workflow_graph(
        [node("start"), node("check"), node("done")],
        [
            edge("start", "check"),
            edge("check", "done"),
            loop_edge("check", "start"),
        ],
        max_nodes=20,
    )

    assert graph.loop_bodies[0].node_keys == ("check", "start")


def test_loop_back_edge_may_share_a_self_edge_pair_only_once() -> None:
    with pytest.raises(WorkflowGraphError) as caught:
        validate_workflow_graph(
            [node("check")],
            [loop_edge("check", "check"), loop_edge("check", "check", condition="on_success")],
            max_nodes=20,
        )

    assert caught.value.code == "duplicate_edge"


def test_graph_without_loop_edges_reports_no_loop_bodies() -> None:
    graph = validate_workflow_graph([node("a"), node("b")], [edge("a", "b")], max_nodes=20)

    assert graph.loop_bodies == ()
