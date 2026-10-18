import asyncio
import json
from contextlib import AsyncExitStack
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.worker import Replayer
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import src.config as config_module

from src.workflows.agent import AgentInput
from src.workflows.multi_agent import (
    LoopBackEdge,
    MultiAgentInput,
    MultiAgentWorkflow,
    _build_previous_iteration,
    build_preparation_schema,
    parse_preparation_briefs,
    _check_output_size,
    _stable_error_code,
)


def _agent(agent_id: str, name: str, model: str = "provider/model") -> dict:
    return {
        "agentDefinitionId": agent_id,
        "name": name,
        "model": model,
        "systemPrompt": f"You are {name}.",
        "temperature": 0.2,
        "allowedSkillIds": [],
        "knowledgeBases": [],
    }


def _snapshot(*, max_parallelism: int = 2) -> dict:
    return {
        "runId": "11111111-1111-1111-1111-111111111111",
        "orgId": "22222222-2222-2222-2222-222222222222",
        "workflowDefinitionId": "33333333-3333-3333-3333-333333333333",
        "workflowDefinitionVersion": 1,
        "conversationId": "44444444-4444-4444-4444-444444444444",
        "turnId": "55555555-5555-5555-5555-555555555555",
        "sourceMessageId": "66666666-6666-6666-6666-666666666666",
        "temporalWorkflowId": "conversation-444:workflow:555",
        "userRequest": "Prepare an evidence-backed answer.",
        "maxParallelism": max_parallelism,
        "supervisor": _agent("supervisor", "Supervisor", "provider/supervisor"),
        "nodes": [
            {
                "nodeKey": "b",
                "displayName": "Beta",
                "instruction": "Find beta evidence.",
                "agent": _agent("agent-b", "Beta"),
            },
            {
                "nodeKey": "a",
                "displayName": "Alpha",
                "instruction": "Find alpha evidence.",
                "agent": _agent("agent-a", "Alpha"),
            },
            {
                "nodeKey": "c",
                "displayName": "Combine",
                "instruction": "Combine direct evidence.",
                "agent": _agent("agent-c", "Combine"),
            },
        ],
        "edges": [
            {"sourceNodeKey": "b", "targetNodeKey": "c"},
            {"sourceNodeKey": "a", "targetNodeKey": "c"},
        ],
        "status": "pending",
        "createdAt": "2026-09-02T00:00:00Z",
    }


def test_preparation_schema_requires_exact_non_empty_node_keys() -> None:
    schema = build_preparation_schema(("b", "a"))

    assert list(schema["properties"]) == ["a", "b"]
    assert schema["required"] == ["a", "b"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["a"] == {"type": "string", "minLength": 1}


@pytest.mark.parametrize(
    "payload",
    [
        '{"a":"brief"}',
        '{"a":"brief","b":"brief","extra":"no"}',
        '{"a":"","b":"brief"}',
        '{"a":"brief","b":2}',
        "not-json",
    ],
)
def test_preparation_briefs_reject_missing_extra_empty_and_invalid_values(
    payload: str,
) -> None:
    with pytest.raises(ValueError, match="invalid_supervisor_output"):
        parse_preparation_briefs(payload, ("a", "b"))


def test_preparation_briefs_returns_stable_exact_mapping() -> None:
    assert parse_preparation_briefs('{"b":" B ","a":"A"}', ("b", "a")) == {
        "a": "A",
        "b": "B",
    }


def test_node_and_aggregate_output_limits_use_utf8_bytes() -> None:
    sizes: dict[str, int] = {}
    _check_output_size("a", "x" * 65_536, sizes)
    assert sizes == {"a": 65_536}

    with pytest.raises(ApplicationError) as node_error:
        _check_output_size("b", "x" * 65_537, {})
    assert node_error.value.type == "output_limit_exceeded"

    with pytest.raises(ApplicationError) as aggregate_error:
        _check_output_size("other", "b", {"a": 524_288})
    assert aggregate_error.value.type == "output_limit_exceeded"


def test_output_size_tracks_per_node_so_a_reset_iteration_does_not_double_count() -> None:
    sizes: dict[str, int] = {}
    _check_output_size("loop_node", "x" * 60_000, sizes)
    _check_output_size("other_node", "y" * 100, sizes)
    # A loop reset clears the tail's prior-iteration entry (see
    # _take_loop_back) before the node runs again — re-recording its output
    # must not be treated as if both iterations' bytes coexist (that would
    # spuriously trip the aggregate limit purely as a function of how many
    # times the loop has repeated).
    sizes.pop("loop_node")
    _check_output_size("loop_node", "x" * 60_000, sizes)
    assert sizes == {"loop_node": 60_000, "other_node": 100}


def test_previous_iteration_snapshot_is_stable_and_status_aware() -> None:
    loop = LoopBackEdge(
        source="review", target="draft", condition="on_failure",
        max_iterations=3, body=frozenset({"review", "draft"}),
    )

    snapshot = _build_previous_iteration(
        loop,
        2,
        {"draft": "completed", "review": "failed"},
        {"draft": "draft-2"},
        {"draft": ("s1", "s1")},
        {"review": "execution_failed"},
        web_tools_enabled=True,
    )

    assert snapshot == {
        "iteration": 2,
        "target_node_key": "draft",
        "tail_node_key": "review",
        "node_results": [
            {
                "node_key": "draft", "status": "completed",
                "output": "draft-2", "source_ids": ["s1"],
            },
            {
                "node_key": "review", "status": "failed",
                "error_code": "execution_failed",
            },
        ],
    }


def test_previous_iteration_snapshot_omits_sources_when_web_is_disabled() -> None:
    loop = LoopBackEdge(
        source="review", target="draft", condition="on_success",
        max_iterations=2, body=frozenset({"review", "draft"}),
    )
    snapshot = _build_previous_iteration(
        loop, 1, {"draft": "completed", "review": "completed"},
        {"draft": "d", "review": "r"}, {"draft": ("s1",)}, {},
        web_tools_enabled=False,
    )

    assert all("source_ids" not in result for result in snapshot["node_results"])


def test_previous_iteration_snapshot_rejects_incomplete_body_state() -> None:
    loop = LoopBackEdge(
        source="review", target="draft", condition="on_failure",
        max_iterations=2, body=frozenset({"review", "draft"}),
    )

    with pytest.raises(ValueError, match="invalid_workflow_snapshot"):
        _build_previous_iteration(
            loop, 1, {"draft": "completed"}, {"draft": "d"}, {}, {},
            web_tools_enabled=True,
        )


def test_untyped_child_quota_error_maps_to_safe_code() -> None:
    error = ApplicationError(
        "Client error '429 Too Many Requests' — quota exceeded",
        non_retryable=True,
    )

    assert _stable_error_code(error) == "quota_exhausted"


@workflow.defn(name="AgentWorkflow")
class RecordingAgentWorkflow:
    @workflow.run
    async def run(self, input: AgentInput) -> str:
        phase = input.workflow_phase
        node_key = input.workflow_node_key
        await workflow.execute_activity(
            "record_agent_start",
            args=(phase, node_key, input.context),
            start_to_close_timeout=timedelta(seconds=10),
        )
        instruction = input.context.get("instruction")
        if instruction == "FAIL":
            raise ApplicationError(
                "quota_exhausted", type="quota_exhausted", non_retryable=True
            )
        if instruction == "BLOCK":
            await workflow.wait_condition(lambda: False)
        if phase == "supervisor_prepare":
            assert input.output_schema is not None
            required = input.output_schema["required"]
            return json.dumps({key: f"brief {key}" for key in required})
        if phase == "node":
            if node_key in {"a", "b"}:
                await workflow.sleep(1)
            await workflow.execute_activity(
                "record_agent_end",
                node_key,
                start_to_close_timeout=timedelta(seconds=10),
            )
            return f"output {node_key}"
        return "final answer"


@workflow.defn(name="AgentWorkflow")
class IterationRecordingAgentWorkflow:
    @workflow.run
    async def run(self, input: AgentInput) -> dict | str:
        if input.workflow_phase == "supervisor_prepare":
            assert input.output_schema is not None
            return {
                "answer": json.dumps({
                    key: f"brief {key}" for key in input.output_schema["required"]
                }),
                "source_ids": ["prep-source"],
            }
        if input.workflow_phase == "supervisor_synthesize":
            return "final answer"
        assert input.workflow_node_key is not None
        await workflow.execute_activity(
            "record_loop_start",
            args=(
                input.workflow_node_key,
                input.execution_iteration,
                input.context,
                input.inherited_source_ids,
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )
        if input.context.get("instruction") == "FAIL":
            raise ApplicationError(
                "quota_exhausted", type="quota_exhausted", non_retryable=True
            )
        return {
            "answer": f"{input.workflow_node_key}-{input.execution_iteration}",
            "source_ids": [f"{input.workflow_node_key}-source"],
        }


@pytest.mark.asyncio
async def test_parallel_dag_uses_lexical_ready_order_and_stable_predecessors() -> None:
    snapshot = _snapshot(max_parallelism=2)
    starts: list[tuple[str, str | None, dict]] = []
    active_nodes: set[str] = set()
    max_active = 0
    authorizations: list[tuple[str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []
    persisted: list[tuple[str, str, str | None, str | None]] = []
    published: list[tuple[str, dict]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        assert run_id == snapshot["runId"]
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        authorizations.append((phase, node_key))

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        nonlocal max_active
        starts.append((phase, node_key, context))
        if phase == "node" and node_key is not None:
            active_nodes.add(node_key)
            max_active = max(max_active, len(active_nodes))

    @activity.defn(name="record_agent_end")
    async def record_agent_end(node_key: str | None) -> None:
        if node_key is not None:
            active_nodes.remove(node_key)

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        published.append((event_type, data))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-test",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                record_agent_end,
                publish_event,
            ],
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(worker)
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-success",
                task_queue="multi-agent-test",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    node_starts = [node_key for phase, node_key, _ in starts if phase == "node"]
    assert node_starts == ["a", "b", "c"]
    assert max_active == 2
    c_context = next(context for phase, key, context in starts if key == "c")
    assert c_context == {
        "user_request": snapshot["userRequest"],
        "instruction": "Combine direct evidence.",
        "supervisor_brief": "brief c",
        "predecessor_outputs": [
            {"node_key": "a", "output": "output a"},
            {"node_key": "b", "output": "output b"},
        ],
    }
    synthesis_context = next(
        context for phase, _, context in starts if phase == "supervisor_synthesize"
    )
    assert synthesis_context["node_outputs"] == [
        {"node_key": "a", "output": "output a"},
        {"node_key": "b", "output": "output b"},
        {"node_key": "c", "output": "output c"},
    ]
    assert authorizations == [
        ("supervisor_prepare", None),
        ("node", "a"),
        ("node", "b"),
        ("node", "c"),
        ("supervisor_synthesize", None),
    ]
    assert transitions == [
        ("planning", None),
        ("running", None),
        ("synthesizing", None),
        ("completed", None),
    ]
    assert result.status == "completed"
    assert result.answer == "final answer"
    assert result.model == "provider/supervisor"
    assert list(result.context_used) == [
        {"node_key": "a", "output": "output a"},
        {"node_key": "b", "output": "output b"},
        {"node_key": "c", "output": "output c"},
    ]
    assert json.loads(next(row[2] for row in persisted if row[:2] == ("supervisor_prepare", "completed"))) == {
        "a": "brief a",
        "b": "brief b",
        "c": "brief c",
    }
    assert [event for event, _ in published] == [
        "workflow-run-status",
        "workflow-run-status",
        "workflow-node-status",
        "workflow-node-status",
        "workflow-node-status",
        "workflow-node-status",
        "workflow-node-status",
        "workflow-node-status",
        "workflow-run-status",
    ]
    for event_type, payload in published:
        allowed = {"turnId", "workflowRunId", "status"}
        if event_type == "workflow-node-status":
            allowed |= {"nodeKey", "nodeName"}
        assert set(payload) <= allowed
        assert payload["workflowRunId"] == snapshot["runId"]
        assert payload["turnId"] == snapshot["turnId"]


@pytest.mark.asyncio
async def test_first_parallel_failure_cancels_sibling_and_skips_later_nodes() -> None:
    snapshot = _snapshot(max_parallelism=2)
    snapshot["nodes"][0]["instruction"] = "FAIL"  # b
    snapshot["nodes"][1]["instruction"] = "BLOCK"  # a
    starts: list[tuple[str, str | None]] = []
    persisted: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        starts.append((phase, node_key))

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-failure",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                publish_event,
            ],
        )
        async with worker:
            result = await env.client.execute_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-failure",
                task_queue="multi-agent-failure",
            )

    assert ("node", "c") not in starts
    assert ("supervisor_synthesize", None) not in starts
    assert ("a", "cancelled", None) in persisted
    assert ("b", "failed", "quota_exhausted") in persisted
    assert transitions[-1] == ("failed", "quota_exhausted")
    assert result.status == "failed"
    assert result.error_code == "quota_exhausted"


@activity.defn(name="record_agent_end")
async def _noop_record_agent_end(node_key: str | None) -> None:
    return None


def _branching_snapshot(nodes: list[dict], edges: list[dict], *, max_parallelism: int = 2) -> dict:
    snapshot = _snapshot(max_parallelism=max_parallelism)
    snapshot["nodes"] = nodes
    snapshot["edges"] = edges
    return snapshot


def _node(node_key: str, instruction: str = "ok") -> dict:
    return {
        "nodeKey": node_key,
        "displayName": node_key.title(),
        "instruction": instruction,
        "agent": _agent(f"agent-{node_key}", node_key.title()),
    }


@pytest.mark.asyncio
async def test_on_failure_edge_runs_fallback_without_cancelling_sibling() -> None:
    # a fails, but it has an on_failure successor (fallback) so the failure is
    # handled: unrelated sibling b keeps running to completion, oksucc (an
    # on_success successor of a) is skipped rather than cancelled, and tail
    # (an on_success successor of fallback) runs normally once fallback
    # completes.
    snapshot = _branching_snapshot(
        nodes=[
            _node("a", "FAIL"),
            _node("b"),
            _node("oksucc"),
            _node("fallback"),
            _node("tail"),
        ],
        edges=[
            {"sourceNodeKey": "a", "targetNodeKey": "oksucc", "condition": "on_success"},
            {"sourceNodeKey": "a", "targetNodeKey": "fallback", "condition": "on_failure"},
            {"sourceNodeKey": "fallback", "targetNodeKey": "tail", "condition": "on_success"},
        ],
        max_parallelism=3,
    )
    starts: list[tuple[str, str | None, dict]] = []
    persisted: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        starts.append((phase, node_key, context))

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-on-failure",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                _noop_record_agent_end,
                publish_event,
            ],
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(worker)
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-on-failure",
                task_queue="multi-agent-on-failure",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert ("a", "failed", "quota_exhausted") in persisted
    assert ("oksucc", "skipped", None) in persisted
    assert ("fallback", "completed", None) in persisted
    assert ("tail", "completed", None) in persisted
    assert ("b", "completed", None) in persisted
    assert ("b", "cancelled", None) not in persisted
    assert ("oksucc", "cancelled", None) not in persisted
    fallback_context = next(
        context for phase, key, context in starts if key == "fallback"
    )
    assert fallback_context["predecessor_outputs"] == [
        {"node_key": "a", "status": "failed", "error_code": "quota_exhausted"}
    ]
    assert result.status == "completed"
    assert result.error_code is None
    assert transitions[-1] == ("completed", None)


@pytest.mark.asyncio
async def test_skip_cascades_transitively_through_a_handled_failure() -> None:
    # a fails (handled by "handler"); b depends on a via on_success and is
    # skipped; c depends on b via on_success and must also be skipped even
    # though its own predecessor never failed — it was itself skipped.
    snapshot = _branching_snapshot(
        nodes=[_node("a", "FAIL"), _node("handler"), _node("b"), _node("c")],
        edges=[
            {"sourceNodeKey": "a", "targetNodeKey": "handler", "condition": "on_failure"},
            {"sourceNodeKey": "a", "targetNodeKey": "b", "condition": "on_success"},
            {"sourceNodeKey": "b", "targetNodeKey": "c", "condition": "on_success"},
        ],
        max_parallelism=2,
    )
    persisted: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        return None

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-skip-cascade",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                _noop_record_agent_end,
                publish_event,
            ],
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(worker)
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-skip-cascade",
                task_queue="multi-agent-skip-cascade",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert ("a", "failed", "quota_exhausted") in persisted
    assert ("handler", "completed", None) in persisted
    assert ("b", "skipped", None) in persisted
    assert ("c", "skipped", None) in persisted
    assert result.status == "completed"
    assert transitions[-1] == ("completed", None)


@pytest.mark.asyncio
async def test_empty_completed_after_skip_and_handled_failures_aborts_before_synthesis() -> None:
    # a fails (handled by b's on_failure edge); z is an on_success successor
    # of a and is skipped; b needs BOTH a's on_failure edge AND z's on_success
    # edge (AND-join) so b is skipped too, since z never completed. Nothing
    # ever completes, so synthesis must never start.
    snapshot = _branching_snapshot(
        nodes=[_node("a", "FAIL"), _node("z"), _node("b")],
        edges=[
            {"sourceNodeKey": "a", "targetNodeKey": "z", "condition": "on_success"},
            {"sourceNodeKey": "a", "targetNodeKey": "b", "condition": "on_failure"},
            {"sourceNodeKey": "z", "targetNodeKey": "b", "condition": "on_success"},
        ],
        max_parallelism=2,
    )
    starts: list[tuple[str, str | None]] = []
    persisted: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        starts.append((phase, node_key))

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-empty-completed",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                _noop_record_agent_end,
                publish_event,
            ],
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(worker)
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-empty-completed",
                task_queue="multi-agent-empty-completed",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert ("a", "failed", "quota_exhausted") in persisted
    assert ("z", "skipped", None) in persisted
    assert ("b", "skipped", None) in persisted
    assert ("supervisor_synthesize", None) not in starts
    assert result.status == "failed"
    assert result.error_code == "invalid_workflow_snapshot"


@pytest.mark.asyncio
async def test_unrelated_all_skip_branch_does_not_abort_an_otherwise_successful_run() -> None:
    # p is an independent node with no relation to the a/z/b skip-chain. If
    # p's own wave finishes (successfully) before a/z/b's skip cascade
    # resolves, self._active can become momentarily empty on an unrelated
    # pass through the scheduling loop while pending is *also* already
    # empty — that must NOT be treated as a deadlock/failure just because
    # nothing happens to be active at that exact moment, since the run
    # already produced real output.
    snapshot = _branching_snapshot(
        nodes=[_node("p"), _node("a", "FAIL"), _node("z"), _node("b")],
        edges=[
            {"sourceNodeKey": "a", "targetNodeKey": "z", "condition": "on_success"},
            {"sourceNodeKey": "a", "targetNodeKey": "b", "condition": "on_failure"},
            {"sourceNodeKey": "z", "targetNodeKey": "b", "condition": "on_success"},
        ],
        max_parallelism=4,
    )
    starts: list[tuple[str, str | None]] = []
    persisted: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        starts.append((phase, node_key))

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-unrelated-skip",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                _noop_record_agent_end,
                publish_event,
            ],
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(worker)
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-unrelated-skip",
                task_queue="multi-agent-unrelated-skip",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert ("p", "completed", None) in persisted
    assert ("a", "failed", "quota_exhausted") in persisted
    assert ("z", "skipped", None) in persisted
    assert ("b", "skipped", None) in persisted
    assert ("supervisor_synthesize", None) in starts
    assert result.status == "completed"
    assert result.error_code is None
    assert transitions[-1] == ("completed", None)


@pytest.mark.asyncio
async def test_authorization_activity_failure_preserves_access_revoked_code() -> None:
    snapshot = _snapshot(max_parallelism=1)
    snapshot["nodes"] = [snapshot["nodes"][1]]
    snapshot["edges"] = []
    persisted: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        if phase == "node":
            raise ApplicationError(
                "access_revoked", type="access_revoked", non_retryable=True
            )

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        return None

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-access-revoked",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                publish_event,
            ],
        )
        async with worker:
            result = await env.client.execute_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-access-revoked",
                task_queue="multi-agent-access-revoked",
            )

    assert ("a", "failed", "access_revoked") in persisted
    assert transitions[-1] == ("failed", "access_revoked")
    assert result.status == "failed"
    assert result.error_code == "access_revoked"


@pytest.mark.asyncio
async def test_authorization_activity_failure_on_a_handled_node_still_runs_fallback() -> None:
    # A pre-launch authorize_workflow_step failure (e.g. a grant revoked
    # between run creation and this node's turn) must go through the same
    # handled/unhandled decision as an execution failure discovered after
    # start_child_workflow — found live against the real stack: the inline
    # (non-gated) launch path called authorize_workflow_step outside any
    # handled-failure check, so it always aborted the run even when an
    # on_failure edge existed.
    snapshot = _branching_snapshot(
        nodes=[_node("a"), _node("fallback")],
        edges=[{"sourceNodeKey": "a", "targetNodeKey": "fallback", "condition": "on_failure"}],
        max_parallelism=2,
    )
    persisted: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        if node_key == "a":
            raise ApplicationError(
                "access_revoked", type="access_revoked", non_retryable=True
            )

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        return None

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-access-revoked-handled",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                _noop_record_agent_end,
                publish_event,
            ],
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(worker)
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-access-revoked-handled",
                task_queue="multi-agent-access-revoked-handled",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert ("a", "failed", "access_revoked") in persisted
    assert ("fallback", "completed", None) in persisted
    assert result.status == "completed"
    assert transitions[-1] == ("completed", None)


@pytest.mark.asyncio
async def test_user_cancellation_returns_cancelled_and_persists_terminal_state() -> None:
    snapshot = _snapshot(max_parallelism=1)
    snapshot["nodes"] = [snapshot["nodes"][1]]
    snapshot["nodes"][0]["instruction"] = "BLOCK"
    snapshot["edges"] = []
    node_started = asyncio.Event()
    persisted: list[tuple[str, str]] = []
    transitions: list[str] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append(status)

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        if node_key == "a":
            node_started.set()

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-cancel",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                publish_event,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-cancel",
                task_queue="multi-agent-cancel",
            )
            await asyncio.wait_for(node_started.wait(), timeout=10)
            await handle.cancel()
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert result.status == "cancelled"
    assert transitions[-1] == "cancelled"
    assert ("a", "cancelled") in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("decision_delay", [0, 130])
async def test_confirmation_gated_node_approved_resumes_normally(decision_delay) -> None:
    snapshot = _snapshot(max_parallelism=1)
    snapshot["nodes"] = [snapshot["nodes"][1]]  # "a"
    snapshot["nodes"][0]["requiresConfirmation"] = True
    snapshot["edges"] = []
    waiting = asyncio.Event()
    persisted: list[tuple[str, str, str | None, str | None]] = []
    transitions: list[tuple[str, str | None]] = []
    refreshed: list[str] = []
    refresh_started = asyncio.Event()
    waiting_events: list[dict] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))
        if phase_key == "a" and status == "waiting_human":
            waiting.set()

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        return None

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        if event_type == "workflow-node-status" and data.get("status") == "waiting_human":
            waiting_events.append(data)

    @activity.defn(name="refresh_runner_activity")
    async def refresh_runner_activity(run_id: str) -> None:
        refreshed.append(run_id)
        refresh_started.set()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-confirm-approve",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                _noop_record_agent_end,
                publish_event,
                refresh_runner_activity,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-confirm-approve",
                task_queue="multi-agent-confirm-approve",
            )
            await asyncio.wait_for(waiting.wait(), timeout=10)
            await asyncio.wait_for(refresh_started.wait(), timeout=10)
            if decision_delay:
                await env.sleep(decision_delay)
            await handle.signal(
                MultiAgentWorkflow.submit_human_decision, args=["a", "approve"]
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert refreshed and all(run_id == snapshot["runId"] for run_id in refreshed)
    if decision_delay:
        assert len(refreshed) >= 2
        assert len(waiting_events) >= 2
    assert ("a", "waiting_human", None, None) in persisted
    assert ("a", "running", None, None) in persisted
    assert ("a", "completed", "output a", None) in persisted
    assert persisted.index(("a", "waiting_human", None, None)) < persisted.index(
        ("a", "running", None, None)
    )
    assert transitions[-1] == ("completed", None)
    assert result.status == "completed"
    assert result.answer == "final answer"


@pytest.mark.asyncio
async def test_confirmation_gated_node_rejected_fails_run_and_cancels_running_sibling() -> None:
    snapshot = _snapshot(max_parallelism=2)
    snapshot["nodes"] = [snapshot["nodes"][0], snapshot["nodes"][1]]  # "b", "a"
    snapshot["nodes"][0]["instruction"] = "BLOCK"  # "b" — non-gated, runs freely
    snapshot["nodes"][1]["requiresConfirmation"] = True  # "a" — gated
    snapshot["edges"] = []
    sibling_started = asyncio.Event()
    waiting = asyncio.Event()
    persisted: list[tuple[str, str, str | None, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))
        if phase_key == "a" and status == "waiting_human":
            waiting.set()

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        if node_key == "b":
            sibling_started.set()

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    @activity.defn(name="refresh_runner_activity")
    async def refresh_runner_activity(run_id: str) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-confirm-reject",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                publish_event,
                refresh_runner_activity,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-confirm-reject",
                task_queue="multi-agent-confirm-reject",
            )
            await asyncio.wait_for(sibling_started.wait(), timeout=10)
            await asyncio.wait_for(waiting.wait(), timeout=10)
            await handle.signal(
                MultiAgentWorkflow.submit_human_decision, args=["a", "reject"]
            )
            result = await handle.result()
            history = await handle.fetch_history()

        sibling_handle = env.client.get_workflow_handle(
            f"{snapshot['runId']}:node:b"
        )
        sibling_description = await sibling_handle.describe()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    # The real remote child workflow was cancelled — not just the local
    # asyncio wrapper task that awaited it.
    assert sibling_description.status == WorkflowExecutionStatus.CANCELED
    assert ("b", "cancelled", None, None) in persisted
    assert ("a", "failed", None, "human_rejected") in persisted
    assert transitions[-1] == ("failed", "human_rejected")
    assert result.status == "failed"
    assert result.error_code == "human_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation_timeout", [5.0, 65.0])
async def test_confirmation_gated_node_times_out_fails_run(
    monkeypatch: pytest.MonkeyPatch, confirmation_timeout: float,
) -> None:
    monkeypatch.setattr(
        config_module.settings, "workflow_confirmation_timeout_seconds", confirmation_timeout
    )
    snapshot = _snapshot(max_parallelism=1)
    snapshot["nodes"] = [snapshot["nodes"][1]]  # "a"
    snapshot["nodes"][0]["requiresConfirmation"] = True
    snapshot["edges"] = []
    persisted: list[tuple[str, str, str | None, str | None]] = []
    transitions: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        transitions.append((status, error_code))

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        return None

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    @activity.defn(name="refresh_runner_activity")
    async def refresh_runner_activity(run_id: str) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-confirm-timeout",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                publish_event,
                refresh_runner_activity,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-confirm-timeout",
                task_queue="multi-agent-confirm-timeout",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert ("a", "failed", None, "confirmation_timeout") in persisted
    assert transitions[-1] == ("failed", "confirmation_timeout")
    assert result.status == "failed"
    assert result.error_code == "confirmation_timeout"


@pytest.mark.asyncio
async def test_mixed_wave_non_gated_node_launches_inline_while_sibling_awaits_confirmation() -> None:
    snapshot = _snapshot(max_parallelism=2)
    snapshot["nodes"] = [snapshot["nodes"][0], snapshot["nodes"][1]]  # "b", "a"
    snapshot["nodes"][1]["requiresConfirmation"] = True  # "a" — gated
    snapshot["edges"] = []
    waiting = asyncio.Event()
    sibling_completed = asyncio.Event()
    persisted: list[tuple[str, str, str | None, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        return None

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        return None

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))
        if phase_key == "a" and status == "waiting_human":
            waiting.set()
        if phase_key == "b" and status == "completed":
            sibling_completed.set()

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        return None

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        return None

    @activity.defn(name="refresh_runner_activity")
    async def refresh_runner_activity(run_id: str) -> None:
        return None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-confirm-mixed",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                _noop_record_agent_end,
                publish_event,
                refresh_runner_activity,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-confirm-mixed",
                task_queue="multi-agent-confirm-mixed",
            )
            # The non-gated sibling completes without waiting on the signal —
            # only the gated node's wave slot stays open on the human wait.
            await asyncio.wait_for(sibling_completed.wait(), timeout=10)
            await asyncio.wait_for(waiting.wait(), timeout=10)
            await handle.signal(
                MultiAgentWorkflow.submit_human_decision, args=["a", "approve"]
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert ("b", "completed", "output b", None) in persisted
    assert ("a", "completed", "output a", None) in persisted
    assert result.status == "completed"


def _loop_snapshot(
    *, max_iterations: int = 3, condition: str = "on_failure", max_parallelism: int = 1
) -> dict:
    """A 2-node loop: start -> check, with check's loop-back edge governing
    both nodes as its body. Mirrors exactly what business-backend's
    workflow_graph validator would compute for this shape.
    """
    return {
        "runId": "77777777-7777-7777-7777-777777777777",
        "orgId": "22222222-2222-2222-2222-222222222222",
        "workflowDefinitionId": "33333333-3333-3333-3333-333333333333",
        "workflowDefinitionVersion": 1,
        "conversationId": "44444444-4444-4444-4444-444444444444",
        "turnId": "55555555-5555-5555-5555-555555555555",
        "sourceMessageId": "66666666-6666-6666-6666-666666666666",
        "temporalWorkflowId": "conversation-444:workflow:555",
        "userRequest": "Iterate until the check passes.",
        "maxParallelism": max_parallelism,
        "supervisor": _agent("supervisor", "Supervisor", "provider/supervisor"),
        "nodes": [
            {
                "nodeKey": "start",
                "displayName": "Start",
                "instruction": "Draft.",
                "agent": _agent("agent-start", "Start"),
            },
            {
                "nodeKey": "check",
                "displayName": "Check",
                "instruction": "FAIL",
                "agent": _agent("agent-check", "Check"),
            },
        ],
        "edges": [
            {"sourceNodeKey": "start", "targetNodeKey": "check"},
            {
                "sourceNodeKey": "check",
                "targetNodeKey": "start",
                "condition": condition,
                "isLoopBack": True,
                "maxIterations": max_iterations,
            },
        ],
        "loopBodies": [
            {
                "sourceNodeKey": "check",
                "targetNodeKey": "start",
                "condition": condition,
                "maxIterations": max_iterations,
                "nodeKeys": ["check", "start"],
            }
        ],
        "status": "pending",
        "createdAt": "2026-09-06T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_loop_body_reruns_until_max_iterations_then_falls_through() -> None:
    snapshot = _loop_snapshot(max_iterations=3, condition="on_failure")
    persisted: list[tuple[str, str, str | None, str | None]] = []
    node_starts: list[str] = []
    authorizations: list[tuple[str, str | None]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        authorizations.append((phase, node_key))

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        pass

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        if phase == "node" and node_key is not None:
            node_starts.append(node_key)

    @activity.defn(name="record_agent_end")
    async def record_agent_end(node_key: str | None) -> None:
        pass

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        pass

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-loop-exhaust",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                record_agent_end,
                publish_event,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-loop-exhaust",
                task_queue="multi-agent-loop-exhaust",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert node_starts == ["start", "check"] * 3
    assert authorizations.count(("node", "start")) == 3
    assert authorizations.count(("node", "check")) == 3
    check_rows = [row for row in persisted if row[0] == "check"]
    assert [row[1] for row in check_rows] == [
        "running", "failed", "pending",
        "running", "failed", "pending",
        "running", "failed",
    ]
    start_rows = [row for row in persisted if row[0] == "start"]
    assert [row[1] for row in start_rows] == [
        "running", "completed", "pending",
        "running", "completed", "pending",
        "running", "completed",
    ]
    # The final (3rd, exhausted) attempt's outcome is what downstream/
    # synthesis sees — "check" stays failed (handled, not fail-fast) and
    # "start"'s last output is what gets synthesized from.
    assert result.status == "completed"
    assert result.answer == "final answer"
    assert list(result.context_used) == [{"node_key": "start", "output": "output start"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("requires_confirmation", [False, True])
async def test_loop_carries_only_previous_loop_iteration(
    requires_confirmation: bool,
) -> None:
    snapshot = _loop_snapshot(max_iterations=3, condition="on_failure")
    snapshot["nodes"][0]["requiresConfirmation"] = requires_confirmation
    starts: list[tuple[str, int, dict, tuple[str, ...]]] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        pass

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        pass

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str, phase_key: str, status: str, output: str | None,
        error_code: str | None,
    ) -> None:
        pass

    @activity.defn(name="record_loop_start")
    async def record_loop_start(
        node_key: str, iteration: int, context: dict,
        inherited_source_ids: tuple[str, ...],
    ) -> None:
        starts.append((node_key, iteration, context, inherited_source_ids))

    @activity.defn(name="refresh_runner_activity")
    async def refresh_runner_activity(run_id: str) -> None:
        pass

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        pass

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-loop-context",
            workflows=[MultiAgentWorkflow, IterationRecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_loop_start,
                publish_event,
                refresh_runner_activity,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"], web_tools_enabled=True),
                id=f"multi-agent-loop-context-{requires_confirmation}",
                task_queue="multi-agent-loop-context",
            )
            if requires_confirmation:
                await handle.signal(
                    MultiAgentWorkflow.submit_human_decision,
                    args=["start", "approve"],
                )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert result.status == "completed"
    start_contexts = [context for key, _, context, _ in starts if key == "start"]
    start_sources = [sources for key, _, _, sources in starts if key == "start"]
    assert "previous_iteration" not in start_contexts[0]
    assert start_sources[0] == ("prep-source",)
    assert start_contexts[1]["previous_iteration"]["iteration"] == 1
    assert start_contexts[1]["previous_iteration"]["node_results"] == [
        {"node_key": "check", "status": "failed", "error_code": "quota_exhausted"},
        {
            "node_key": "start", "status": "completed", "output": "start-1",
            "source_ids": ["start-source"],
        },
    ]
    assert start_sources[1] == ("prep-source", "start-source")
    assert start_contexts[2]["previous_iteration"]["iteration"] == 2
    assert start_contexts[2]["previous_iteration"]["node_results"] == [
        {"node_key": "check", "status": "failed", "error_code": "quota_exhausted"},
        {
            "node_key": "start", "status": "completed", "output": "start-2",
            "source_ids": ["start-source"],
        },
    ]
    assert start_sources[2] == ("prep-source", "start-source")
    assert "start-1" not in repr(start_contexts[2])


@pytest.mark.asyncio
async def test_loop_does_not_fire_when_outcome_does_not_match_condition() -> None:
    # The loop only cares about on_success outcomes; this node always fails,
    # so the loop-back edge is never taken — it falls straight through to
    # its ordinary on_failure edge instead, exactly once.
    snapshot = _loop_snapshot(max_iterations=5, condition="on_success")
    snapshot["nodes"] = [
        {
            "nodeKey": "check",
            "displayName": "Check",
            "instruction": "FAIL",
            "agent": _agent("agent-check", "Check"),
        },
        {
            "nodeKey": "recover",
            "displayName": "Recover",
            "instruction": "Recover.",
            "agent": _agent("agent-recover", "Recover"),
        },
    ]
    snapshot["edges"] = [
        {
            "sourceNodeKey": "check",
            "targetNodeKey": "check",
            "condition": "on_success",
            "isLoopBack": True,
            "maxIterations": 5,
        },
        {"sourceNodeKey": "check", "targetNodeKey": "recover", "condition": "on_failure"},
    ]
    snapshot["loopBodies"] = [
        {
            "sourceNodeKey": "check",
            "targetNodeKey": "check",
            "condition": "on_success",
            "maxIterations": 5,
            "nodeKeys": ["check"],
        }
    ]
    persisted: list[tuple[str, str, str | None, str | None]] = []
    node_starts: list[str] = []

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        pass

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        pass

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))

    @activity.defn(name="record_agent_start")
    async def record_agent_start(
        phase: str, node_key: str | None, context: dict
    ) -> None:
        if phase == "node" and node_key is not None:
            node_starts.append(node_key)

    @activity.defn(name="record_agent_end")
    async def record_agent_end(node_key: str | None) -> None:
        pass

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        pass

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-loop-no-fire",
            workflows=[MultiAgentWorkflow, RecordingAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                record_agent_start,
                record_agent_end,
                publish_event,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-loop-no-fire",
                task_queue="multi-agent-loop-no-fire",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert node_starts == ["check", "recover"]
    assert ("check", "pending", None, None) not in persisted
    assert result.status == "completed"
    assert result.answer == "final answer"


@workflow.defn(name="AgentWorkflow")
class FailOnceThenBlockAgentWorkflow:
    @workflow.run
    async def run(self, input: AgentInput) -> str:
        phase = input.workflow_phase
        node_key = input.workflow_node_key
        if phase == "supervisor_prepare":
            required = input.output_schema["required"] if input.output_schema else []
            return json.dumps({key: f"brief {key}" for key in required})
        if phase == "node":
            count = await workflow.execute_activity(
                "record_and_count",
                node_key,
                start_to_close_timeout=timedelta(seconds=10),
            )
            if count == 1:
                raise ApplicationError(
                    "quota_exhausted", type="quota_exhausted", non_retryable=True
                )
            await workflow.wait_condition(lambda: False)
        return "final answer"


@pytest.mark.asyncio
async def test_cancel_mid_loop_cancels_child_and_does_not_resume() -> None:
    snapshot = _loop_snapshot(max_iterations=5, condition="on_failure")
    snapshot["nodes"] = [
        {
            "nodeKey": "check",
            "displayName": "Check",
            "instruction": "loop-then-block",
            "agent": _agent("agent-check", "Check"),
        }
    ]
    snapshot["edges"] = [
        {
            "sourceNodeKey": "check",
            "targetNodeKey": "check",
            "condition": "on_failure",
            "isLoopBack": True,
            "maxIterations": 5,
        }
    ]
    snapshot["loopBodies"] = [
        {
            "sourceNodeKey": "check",
            "targetNodeKey": "check",
            "condition": "on_failure",
            "maxIterations": 5,
            "nodeKeys": ["check"],
        }
    ]
    persisted: list[tuple[str, str, str | None, str | None]] = []
    counts: dict[str, int] = {}
    blocked = asyncio.Event()

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(
        run_id: str, phase: str, node_key: str | None
    ) -> None:
        pass

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(
        run_id: str, status: str, error_code: str | None
    ) -> None:
        pass

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> None:
        persisted.append((phase_key, status, output, error_code))

    @activity.defn(name="publish_event")
    async def publish_event(event_type: str, data: dict) -> None:
        pass

    @activity.defn(name="record_and_count")
    async def record_and_count(node_key: str) -> int:
        counts[node_key] = counts.get(node_key, 0) + 1
        if counts[node_key] == 2:
            # The child is about to block forever (wait_condition(False)) —
            # safe to cancel the run now.
            blocked.set()
        return counts[node_key]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="multi-agent-loop-cancel",
            workflows=[MultiAgentWorkflow, FailOnceThenBlockAgentWorkflow],
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                publish_event,
                record_and_count,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(run_id=snapshot["runId"]),
                id="multi-agent-loop-cancel",
                task_queue="multi-agent-loop-cancel",
            )
            await asyncio.wait_for(blocked.wait(), timeout=10)
            await handle.cancel()
            result = await handle.result()
            history = await handle.fetch_history()

        sibling_handle = env.client.get_workflow_handle(
            f"{snapshot['runId']}:node:check"
        )
        sibling_description = await sibling_handle.describe()

    await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(history)

    assert result.status == "cancelled"
    assert sibling_description.status == WorkflowExecutionStatus.CANCELED
    # Exactly one loop-back happened (fail on attempt 1, block on attempt 2)
    # — cancellation during attempt 2 must not let a 3rd attempt start.
    assert counts["check"] == 2
    assert ("check", "cancelled", None, None) in persisted
