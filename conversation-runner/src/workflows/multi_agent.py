import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Sequence, cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from src.workflows.lifecycle import register_lifecycle_execution

with workflow.unsafe.imports_passed_through():
    from src.config import settings
    from src.workflows.agent import AgentExecutionSnapshot, AgentInput, AgentResult


_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_NODE_OUTPUT_LIMIT = 65_536
_AGGREGATE_OUTPUT_LIMIT = 524_288


@dataclass(frozen=True)
class NodeDependency:
    source: str
    condition: Literal["on_success", "on_failure"] = "on_success"


@dataclass(frozen=True)
class LoopBackEdge:
    """A loop-back edge, kept entirely separate from NodeDependency/
    predecessors. Folding one into predecessors the way an ordinary edge is
    would make the loop's entry node permanently wait on its own tail's
    outcome, which itself can never arrive without the entry running first —
    a real deadlock in the AND-join, not just a modelling inconvenience.
    `body` (every node key the loop governs, target..source inclusive) and
    `condition`/`max_iterations` are precomputed by business-backend's
    workflow_graph validator and carried in the run snapshot's
    `loopBodies`, keyed here by the loop's tail (source) node.
    """

    source: str
    target: str
    condition: Literal["on_success", "on_failure"]
    max_iterations: int
    body: frozenset[str]


@dataclass(frozen=True)
class MultiAgentNode:
    node_key: str
    display_name: str
    instruction: str
    agent: AgentExecutionSnapshot
    predecessors: tuple[NodeDependency, ...]
    requires_confirmation: bool = False


@dataclass(frozen=True)
class MultiAgentInput:
    run_id: str
    user_questions_enabled: bool = False
    web_tools_enabled: bool = False
    # Image tools for node agents; supervisors never get them.
    image_generation_enabled: bool = False
    image_editing_enabled: bool = False
    recent_images: tuple[dict[str, str], ...] = ()
    # Images the user attached to the message that started the run.
    image_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultiAgentResult:
    status: Literal["completed", "failed", "cancelled"]
    answer: str | None = None
    model: str | None = None
    context_used: Sequence[dict[str, Any]] = ()
    error_code: str | None = None
    source_ids: tuple[str, ...] = ()
    unsupported_source_ids: tuple[str, ...] = ()
    image_ids: tuple[str, ...] = ()


def _agent_result(value: Any) -> AgentResult:
    if isinstance(value, AgentResult):
        return value
    if isinstance(value, str):
        return AgentResult(answer=value)
    if isinstance(value, dict):
        answer = value.get("answer")
        source_ids = value.get("source_ids", value.get("sourceIds", ()))
        unsupported = value.get(
            "unsupported_source_ids",
            value.get("unsupportedSourceIds", ()),
        )
        image_ids = value.get("image_ids", value.get("imageIds", ()))
        if (
            not isinstance(answer, str)
            or not isinstance(source_ids, (list, tuple))
            or not isinstance(unsupported, (list, tuple))
            or not isinstance(image_ids, (list, tuple))
            or any(
                not isinstance(item, str) for item in (*source_ids, *unsupported, *image_ids)
            )
        ):
            raise ValueError("invalid_agent_result")
        return AgentResult(
            answer=answer,
            source_ids=tuple(source_ids),
            unsupported_source_ids=tuple(unsupported),
            image_ids=tuple(image_ids),
        )
    raise ValueError("invalid_agent_result")


def build_preparation_schema(node_keys: Sequence[str]) -> dict[str, Any]:
    ordered = sorted(node_keys)
    return {
        "type": "object",
        "properties": {
            node_key: {"type": "string", "minLength": 1} for node_key in ordered
        },
        "required": ordered,
        "additionalProperties": False,
    }


def parse_preparation_briefs(raw: str, node_keys: Sequence[str]) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_supervisor_output") from exc
    expected = set(node_keys)
    if not isinstance(parsed, dict) or set(parsed) != expected:
        raise ValueError("invalid_supervisor_output")
    briefs: dict[str, str] = {}
    for node_key in sorted(expected):
        value = parsed[node_key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid_supervisor_output")
        briefs[node_key] = value.strip()
    return briefs


def _agent_snapshot(raw: dict[str, Any]) -> AgentExecutionSnapshot:
    knowledge_bases = raw.get("knowledgeBases", [])
    return AgentExecutionSnapshot(
        agent_definition_id=str(raw["agentDefinitionId"]),
        name=str(raw["name"]),
        model=str(raw["model"]),
        system_prompt=str(raw["systemPrompt"]),
        temperature=float(raw["temperature"]),
        allowed_skill_ids=tuple(str(value) for value in raw.get("allowedSkillIds", [])),
        knowledge_bases=tuple(
            {"id": str(item["id"]), "name": str(item["name"])}
            for item in knowledge_bases
        ),
    )


def _parse_snapshot(
    raw: dict[str, Any],
) -> tuple[
    str,
    int,
    AgentExecutionSnapshot,
    tuple[MultiAgentNode, ...],
    dict[str, LoopBackEdge],
]:
    node_keys = {str(item["nodeKey"]) for item in raw["nodes"]}
    predecessors: dict[str, list[NodeDependency]] = {
        node_key: [] for node_key in node_keys
    }
    for edge in raw.get("edges", []):
        source = str(edge["sourceNodeKey"])
        target = str(edge["targetNodeKey"])
        condition = str(edge.get("condition", "on_success"))
        if source not in node_keys or target not in node_keys:
            raise ValueError("invalid_workflow_snapshot")
        if condition not in {"on_success", "on_failure"}:
            raise ValueError("invalid_workflow_snapshot")
        if edge.get("isLoopBack"):
            # Never a scheduling dependency — see LoopBackEdge. The governed
            # body/limits arrive separately via loopBodies below.
            continue
        predecessors[target].append(
            NodeDependency(source=source, condition=cast(Any, condition))
        )
    loop_edges: dict[str, LoopBackEdge] = {}
    for item in raw.get("loopBodies", []):
        source = str(item["sourceNodeKey"])
        target = str(item["targetNodeKey"])
        condition = str(item["condition"])
        body = frozenset(str(key) for key in item["nodeKeys"])
        if (
            condition not in {"on_success", "on_failure"}
            or source not in node_keys
            or target not in node_keys
            or not body <= node_keys
        ):
            raise ValueError("invalid_workflow_snapshot")
        loop_edges[source] = LoopBackEdge(
            source=source,
            target=target,
            condition=cast(Any, condition),
            max_iterations=int(item["maxIterations"]),
            body=body,
        )
    nodes = tuple(
        MultiAgentNode(
            node_key=str(item["nodeKey"]),
            display_name=str(item["displayName"]),
            instruction=str(item["instruction"]),
            agent=_agent_snapshot(item["agent"]),
            predecessors=tuple(
                sorted(
                    predecessors[str(item["nodeKey"])],
                    key=lambda dep: dep.source,
                )
            ),
            requires_confirmation=bool(item.get("requiresConfirmation", False)),
        )
        for item in sorted(raw["nodes"], key=lambda value: str(value["nodeKey"]))
    )
    parallelism = int(raw["maxParallelism"])
    if not nodes or parallelism < 1 or parallelism > 4:
        raise ValueError("invalid_workflow_snapshot")
    return (
        str(raw["userRequest"]),
        parallelism,
        _agent_snapshot(raw["supervisor"]),
        nodes,
        loop_edges,
    )


def _nodes_with_failure_handler(
    nodes: tuple[MultiAgentNode, ...], loop_edges: dict[str, LoopBackEdge]
) -> set[str]:
    handler_keys = {
        dependency.source
        for node in nodes
        for dependency in node.predecessors
        if dependency.condition == "on_failure"
    }
    # A loop tail's on_failure loop-back edge is a failure handler too (it's
    # deliberately never in `predecessors`, see LoopBackEdge) — otherwise the
    # very first failure it's meant to retry would fail-fast the whole run
    # before the loop ever gets a chance to fire.
    handler_keys.update(
        tail for tail, loop in loop_edges.items() if loop.condition == "on_failure"
    )
    return handler_keys


def _resolve_ready_and_skips(
    pending: set[str],
    outcomes: dict[str, str],
    nodes_by_key: dict[str, MultiAgentNode],
) -> tuple[list[str], list[str]]:
    newly_skipped: list[str] = []
    changed = True
    while changed:
        changed = False
        for node_key in sorted(pending):
            deps = nodes_by_key[node_key].predecessors
            statuses = [outcomes.get(dependency.source) for dependency in deps]
            if any(status is None for status in statuses):
                continue
            satisfied = all(
                status == ("completed" if dependency.condition == "on_success" else "failed")
                for dependency, status in zip(deps, statuses)
            )
            if not satisfied:
                outcomes[node_key] = "skipped"
                newly_skipped.append(node_key)
                pending.discard(node_key)
                changed = True
    ready = sorted(
        node_key
        for node_key in pending
        if all(
            outcomes.get(dependency.source)
            == ("completed" if dependency.condition == "on_success" else "failed")
            for dependency in nodes_by_key[node_key].predecessors
        )
    )
    return ready, newly_skipped


def _stable_error_code(exc: BaseException) -> str:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ApplicationError):
            if current.type in {
                "access_revoked",
                "quota_exhausted",
                "workflow_unavailable",
                "output_limit_exceeded",
                "invalid_supervisor_output",
                "human_rejected",
                "confirmation_timeout",
                "user_input_timeout",
                "user_input_cancelled",
                "user_input_limit",
                "authorization_revoked",
                "builtin_round_limit_exceeded",
            }:
                return current.type
            message = str(current).lower()
            if "429" in message or "quota" in message:
                return "quota_exhausted"
            if "403" in message or "404" in message:
                return "access_revoked"
        if isinstance(current, ValueError) and str(current) in {
            "invalid_supervisor_output",
            "invalid_workflow_snapshot",
        }:
            return str(current)
        current = current.__cause__
    return "execution_failed"


def _check_output_size(node_key: str, output: str, output_sizes: dict[str, int]) -> None:
    # Per-node sizes, not a running scalar total: a loop body node's output
    # is overwritten every iteration (same reused row, see
    # workflow_run_repository.py), so summing a dict keyed by node_key
    # mirrors the DB-side check (which recomputes SUM(...) fresh and only
    # ever reflects each node's latest output) instead of double-counting
    # every earlier iteration's already-superseded output.
    size = len(output.encode("utf-8"))
    if size > _NODE_OUTPUT_LIMIT:
        raise ApplicationError(
            "output_limit_exceeded",
            type="output_limit_exceeded",
            non_retryable=True,
        )
    aggregate = sum(value for key, value in output_sizes.items() if key != node_key) + size
    if aggregate > _AGGREGATE_OUTPUT_LIMIT:
        raise ApplicationError(
            "output_limit_exceeded",
            type="output_limit_exceeded",
            non_retryable=True,
        )
    output_sizes[node_key] = size


def _should_loop_back(
    node_key: str,
    outcome: str,
    loop_edges: dict[str, LoopBackEdge],
    loop_iterations: dict[str, int],
) -> bool:
    loop = loop_edges.get(node_key)
    if loop is None:
        return False
    expected = "completed" if loop.condition == "on_success" else "failed"
    if outcome != expected:
        return False
    # default 1: the body has already run once by the time this is first consulted.
    if loop_iterations.get(node_key, 1) >= loop.max_iterations:
        return False
    return True


def _build_predecessor_outputs(
    node: MultiAgentNode,
    completed: dict[str, str],
    failed_error_codes: dict[str, str],
) -> list[dict[str, str]]:
    outputs: list[dict[str, str]] = []
    for dependency in node.predecessors:
        if dependency.condition == "on_success":
            outputs.append(
                {"node_key": dependency.source, "output": completed[dependency.source]}
            )
        else:
            outputs.append(
                {
                    "node_key": dependency.source,
                    "status": "failed",
                    "error_code": failed_error_codes[dependency.source],
                }
            )
    return outputs


def _build_previous_iteration(
    loop: LoopBackEdge,
    iteration: int,
    outcomes: dict[str, str],
    completed: dict[str, str],
    completed_source_ids: dict[str, tuple[str, ...]],
    failed_error_codes: dict[str, str],
    *,
    web_tools_enabled: bool,
) -> dict[str, Any]:
    node_results: list[dict[str, Any]] = []
    for node_key in sorted(loop.body):
        status = outcomes.get(node_key)
        if status == "completed" and node_key in completed:
            result: dict[str, Any] = {
                "node_key": node_key,
                "status": "completed",
                "output": completed[node_key],
            }
            if web_tools_enabled:
                result["source_ids"] = list(
                    dict.fromkeys(completed_source_ids.get(node_key, ()))
                )
        elif status == "failed" and node_key in failed_error_codes:
            result = {
                "node_key": node_key,
                "status": "failed",
                "error_code": failed_error_codes[node_key],
            }
        else:
            raise ValueError("invalid_workflow_snapshot")
        node_results.append(result)
    return {
        "iteration": iteration,
        "target_node_key": loop.target,
        "tail_node_key": loop.source,
        "node_results": node_results,
    }


def _previous_iteration_for_node(
    node_key: str,
    loop_edges: dict[str, LoopBackEdge],
    previous_loop_iterations: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for tail_key in sorted(loop_edges):
        if node_key in loop_edges[tail_key].body:
            return previous_loop_iterations.get(tail_key)
    return None


def _previous_iteration_source_ids(
    previous_iteration: dict[str, Any] | None,
) -> tuple[str, ...]:
    if previous_iteration is None:
        return ()
    return tuple(
        dict.fromkeys(
            source_id
            for result in previous_iteration["node_results"]
            for source_id in result.get("source_ids", [])
        )
    )


async def _activity(name: str, args: tuple[Any, ...]) -> Any:
    return await workflow.execute_activity(
        name,
        args=args,
        start_to_close_timeout=_ACTIVITY_TIMEOUT,
        retry_policy=_ACTIVITY_RETRY_POLICY,
    )


def _node_agent_input(
    input: MultiAgentInput,
    node_key: str,
    node: MultiAgentNode,
    user_request: str,
    brief: str,
    predecessor_outputs: list[dict[str, Any]],
    previous_iteration: dict[str, Any] | None,
    turn_id: str,
    execution_iteration: int,
    inherited_source_ids: tuple[str, ...],
    produced_images: Sequence[str],
) -> AgentInput:
    """One builder for inline and confirmation-gated nodes, so both get the
    same tools and context."""
    images_enabled = input.image_generation_enabled or input.image_editing_enabled
    # Earlier nodes' images first (newest first), so a later node can edit them.
    recent_images = (
        tuple(
            {"id": image_id, "prompt": "", "origin": "generated"}
            for image_id in reversed(produced_images)
        )
        + input.recent_images
        if images_enabled
        else ()
    )
    return AgentInput(
        agent=node.agent,
        goal=(
            f"User request:\n{user_request}\n\n"
            f"Node instruction:\n{node.instruction}\n\n"
            f"Supervisor brief:\n{brief}"
        ),
        context={
            "user_request": user_request,
            "instruction": node.instruction,
            "supervisor_brief": brief,
            "predecessor_outputs": predecessor_outputs,
            **({"previous_iteration": previous_iteration} if previous_iteration is not None else {}),
        },
        event_visibility="internal",
        workflow_run_id=input.run_id,
        workflow_phase="node",
        workflow_node_key=node_key,
        turn_id=turn_id,
        execution_iteration=execution_iteration,
        user_questions_enabled=input.user_questions_enabled,
        web_tools_enabled=input.web_tools_enabled,
        inherited_source_ids=inherited_source_ids,
        image_generation_enabled=input.image_generation_enabled,
        image_editing_enabled=input.image_editing_enabled,
        recent_images=recent_images,
        image_ids=input.image_ids,
    )


@workflow.defn(name="MultiAgentWorkflow")
class MultiAgentWorkflow:
    def __init__(self) -> None:
        self._active: dict[str, Any] = {}
        self._run_id = ""
        self._turn_id = ""
        self._node_names: dict[str, str] = {}
        self._decisions: dict[str, str] = {}
        # Images per completed node (reset with a loop body) and every image
        # the run produced, in creation order.
        self._node_image_ids: dict[str, tuple[str, ...]] = {}
        self._produced_images: list[str] = []

    @workflow.signal
    def submit_human_decision(self, node_key: str, decision: str) -> None:
        self._decisions.setdefault(node_key, decision)

    async def _publish_run_status(self, status: str) -> None:
        await _activity(
            "publish_event",
            (
                "workflow-run-status",
                {
                    "turnId": self._turn_id,
                    "workflowRunId": self._run_id,
                    "status": status,
                },
            ),
        )

    async def _publish_node_status(
        self,
        node_key: str,
        status: str,
        error_code: str | None = None,
    ) -> None:
        payload = {
            "turnId": self._turn_id,
            "workflowRunId": self._run_id,
            "nodeKey": node_key,
            "nodeName": self._node_names[node_key],
            "status": status,
        }
        if error_code is not None:
            payload["errorCode"] = error_code
        await _activity(
            "publish_event",
            (
                "workflow-node-status",
                payload,
            ),
        )

    async def _persist(
        self,
        run_id: str,
        phase_key: str,
        status: str,
        output: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await _activity(
            "persist_workflow_node_result",
            (run_id, phase_key, status, output, error_code),
        )

    async def _run_gated_node(
        self,
        run_id: str,
        node_key: str,
        node: MultiAgentNode,
        user_request: str,
        briefs: dict[str, str],
        completed: dict[str, str],
        failed_error_codes: dict[str, str],
        execution_iteration: int,
        input: MultiAgentInput,
        inherited_source_ids: tuple[str, ...],
        previous_iteration: dict[str, Any] | None,
    ) -> Any:
        # Confirmation-gated nodes get their entire lifecycle (wait + setup +
        # launch) deferred into this task — the human wait is unbounded up to
        # workflow_confirmation_timeout_seconds and must not block siblings
        # from launching. Non-gated nodes deliberately do NOT go through this
        # coroutine: launching them inline in the scheduling loop keeps their
        # authorize/persist/publish "running" transitions in stable lexical
        # order across a wave, matching pre-HITL behavior. Mixing a gated and
        # a non-gated node in the same wave still races between them — only
        # an all-non-gated wave gets the ordering guarantee.
        await self._persist(run_id, node_key, "waiting_human")
        await self._publish_node_status(node_key, "waiting_human")
        await _activity("refresh_runner_activity", (run_id,))
        try:
            timeout = timedelta(seconds=settings.workflow_confirmation_timeout_seconds)
            if workflow.patched("hitl-progress-heartbeats-v1"):
                # Keep the browser subscription and runner idle lease alive,
                # but never reset the human decision deadline. Repeated safe
                # node status carries no prompts, outputs or credentials.
                deadline = workflow.now() + timeout
                while node_key not in self._decisions:
                    remaining = deadline - workflow.now()
                    if remaining <= timedelta(0):
                        raise asyncio.TimeoutError
                    try:
                        await workflow.wait_condition(
                            lambda: node_key in self._decisions,
                            timeout=min(timedelta(seconds=30), remaining),
                        )
                    except asyncio.TimeoutError:
                        if workflow.now() >= deadline:
                            raise
                        await _activity("refresh_runner_activity", (run_id,))
                        await self._publish_node_status(node_key, "waiting_human")
            else:
                # Existing histories keep their original single wait command.
                await workflow.wait_condition(
                    lambda: node_key in self._decisions, timeout=timeout
                )
        except asyncio.TimeoutError as exc:
            raise ApplicationError(
                "confirmation_timeout",
                type="confirmation_timeout",
                non_retryable=True,
            ) from exc
        if self._decisions[node_key] == "reject":
            raise ApplicationError(
                "human_rejected", type="human_rejected", non_retryable=True
            )

        await _activity("authorize_workflow_step", (run_id, "node", node_key))
        await self._persist(run_id, node_key, "running")
        await self._publish_node_status(node_key, "running")
        predecessor_outputs = _build_predecessor_outputs(
            node, completed, failed_error_codes
        )
        handle = await workflow.start_child_workflow(
            "AgentWorkflow",
            _node_agent_input(
                input,
                node_key,
                node,
                user_request,
                briefs[node_key],
                predecessor_outputs,
                previous_iteration,
                self._turn_id,
                execution_iteration,
                inherited_source_ids,
                self._produced_images,
            ),
            id=f"{run_id}:node:{node_key}",
        )
        return await handle

    async def _cancel_active(self, run_id: str) -> set[str]:
        # Accepted limitation: a loop body node reset to `pending` between
        # iterations (see _take_loop_back) is only ever in `self._active`
        # while it's actually running one of those iterations — if
        # cancellation lands in the brief in-memory window between one
        # iteration's completion and the next iteration's launch, this loop
        # only cancels what's currently active, so that node's last
        # persisted status may briefly still show the prior iteration's
        # outcome until the run's own failure/cancel path settles. Not a
        # Temporal determinism issue, just an observability gap.
        active = [(key, self._active[key]) for key in sorted(self._active)]
        cancelled_keys = {
            key for key, handle in active if not handle.done() or handle.cancelled()
        }
        for key, handle in active:
            if key not in cancelled_keys:
                continue
            if not handle.done():
                handle.cancel()
        if active:
            await workflow.wait(tuple(handle for _, handle in active))
        for phase_key in sorted(cancelled_keys):
            try:
                await self._persist(run_id, phase_key, "cancelled")
                if phase_key in self._node_names:
                    await self._publish_node_status(phase_key, "cancelled")
            except Exception:
                workflow.logger.warning(f"Could not persist cancellation for {phase_key}")
        self._active.clear()
        return cancelled_keys

    async def _take_loop_back(
        self,
        run_id: str,
        loop: LoopBackEdge,
        pending: set[str],
        outcomes: dict[str, str],
        completed: dict[str, str],
        completed_source_ids: dict[str, tuple[str, ...]],
        failed_error_codes: dict[str, str],
        output_sizes: dict[str, int],
    ) -> None:
        for body_key in sorted(loop.body):
            outcomes.pop(body_key, None)
            completed.pop(body_key, None)
            completed_source_ids.pop(body_key, None)
            self._node_image_ids.pop(body_key, None)
            failed_error_codes.pop(body_key, None)
            output_sizes.pop(body_key, None)
            pending.add(body_key)
            # Persisted immediately (not left in-memory until the next
            # iteration launches) so diagnostics don't keep showing a stale
            # prior-iteration terminal status if the run fails/cancels
            # before the next iteration completes.
            await self._persist(run_id, body_key, "pending")

    async def _finalize_node_outcome(
        self,
        run_id: str,
        node_key: str,
        outcome: str,
        loop_edges: dict[str, LoopBackEdge],
        loop_iterations: dict[str, int],
        previous_loop_iterations: dict[str, dict[str, Any]],
        pending: set[str],
        outcomes: dict[str, str],
        completed: dict[str, str],
        completed_source_ids: dict[str, tuple[str, ...]],
        failed_error_codes: dict[str, str],
        output_sizes: dict[str, int],
        web_tools_enabled: bool,
    ) -> None:
        if not _should_loop_back(node_key, outcome, loop_edges, loop_iterations):
            return
        loop = loop_edges[node_key]
        if workflow.patched("loop-iteration-context-v1"):
            previous_loop_iterations[node_key] = _build_previous_iteration(
                loop,
                loop_iterations.get(node_key, 1),
                outcomes,
                completed,
                completed_source_ids,
                failed_error_codes,
                web_tools_enabled=web_tools_enabled,
            )
        await self._take_loop_back(
            run_id,
            loop,
            pending,
            outcomes,
            completed,
            completed_source_ids,
            failed_error_codes,
            output_sizes,
        )
        loop_iterations[node_key] = loop_iterations.get(node_key, 1) + 1

    async def _fail(
        self,
        run_id: str,
        error_code: str,
        phase_key: str | None = None,
    ) -> MultiAgentResult:
        await self._cancel_active(run_id)
        if phase_key is not None:
            try:
                await self._persist(run_id, phase_key, "failed", error_code=error_code)
                if phase_key in self._node_names:
                    await self._publish_node_status(
                        phase_key, "failed", error_code
                    )
            except Exception:
                workflow.logger.warning(f"Could not persist failure for {phase_key}")
        try:
            await _activity("transition_workflow_run", (run_id, "failed", error_code))
        except Exception:
            workflow.logger.warning("Could not persist failed workflow transition")
        return MultiAgentResult(status="failed", error_code=error_code)

    @workflow.run
    async def run(self, input: MultiAgentInput) -> MultiAgentResult:
        await register_lifecycle_execution()
        run_id = input.run_id
        current_phase: str | None = None
        output_sizes: dict[str, int] = {}
        try:
            snapshot = await _activity("load_workflow_run_snapshot", (run_id,))
            if not isinstance(snapshot, dict):
                raise ValueError("invalid_workflow_snapshot")
            user_request, parallelism, supervisor, nodes, loop_edges = _parse_snapshot(
                snapshot
            )
            nodes_by_key = {node.node_key: node for node in nodes}
            self._run_id = run_id
            self._turn_id = str(snapshot["turnId"])
            self._node_names = {
                node.node_key: node.display_name for node in nodes
            }

            current_phase = "supervisor_prepare"
            await _activity(
                "authorize_workflow_step", (run_id, "supervisor_prepare", None)
            )
            await _activity("transition_workflow_run", (run_id, "planning", None))
            await self._publish_run_status("planning")
            await self._persist(run_id, current_phase, "running")
            preparation_handle = await workflow.start_child_workflow(
                "AgentWorkflow",
                AgentInput(
                    agent=supervisor,
                    goal="Prepare one concise execution brief for every workflow node.",
                    context={"user_request": user_request},
                    output_schema=build_preparation_schema(tuple(nodes_by_key)),
                    event_visibility="internal",
                    workflow_run_id=run_id,
                    workflow_phase="supervisor_prepare",
                    turn_id=self._turn_id,
                    user_questions_enabled=input.user_questions_enabled,
                    web_tools_enabled=input.web_tools_enabled,
                    image_ids=input.image_ids,
                ),
                id=f"{run_id}:supervisor_prepare",
            )
            self._active[current_phase] = preparation_handle
            preparation_raw = await preparation_handle
            self._active.pop(current_phase, None)
            preparation_result = _agent_result(preparation_raw)
            briefs = parse_preparation_briefs(
                preparation_result.answer,
                tuple(nodes_by_key),
            )
            canonical_briefs = json.dumps(briefs, sort_keys=True, separators=(",", ":"))
            _check_output_size("supervisor_prepare", canonical_briefs, output_sizes)
            await self._persist(run_id, current_phase, "completed", canonical_briefs)
            current_phase = None

            await _activity("transition_workflow_run", (run_id, "running", None))
            await self._publish_run_status("running")
            completed: dict[str, str] = {}
            completed_source_ids: dict[str, tuple[str, ...]] = {}
            failed_error_codes: dict[str, str] = {}
            outcomes: dict[str, str] = {}
            loop_iterations: dict[str, int] = {}
            previous_loop_iterations: dict[str, dict[str, Any]] = {}
            execution_iterations: dict[str, int] = {}
            nodes_with_failure_handler = _nodes_with_failure_handler(nodes, loop_edges)
            pending = set(nodes_by_key)

            while pending or self._active:
                ready, newly_skipped = _resolve_ready_and_skips(
                    pending, outcomes, nodes_by_key
                )
                for node_key in newly_skipped:
                    await self._persist(run_id, node_key, "skipped")
                    await self._publish_node_status(node_key, "skipped")

                progressed = bool(newly_skipped)
                for node_key in ready[: max(0, parallelism - len(self._active))]:
                    node = nodes_by_key[node_key]
                    previous_iteration = _previous_iteration_for_node(
                        node_key, loop_edges, previous_loop_iterations
                    )
                    inherited_source_ids = tuple(
                        dict.fromkeys(
                            (
                                *preparation_result.source_ids,
                                *_previous_iteration_source_ids(previous_iteration),
                            )
                        )
                    )
                    execution_iterations[node_key] = execution_iterations.get(node_key, 0) + 1
                    if node.requires_confirmation:
                        self._active[node_key] = asyncio.ensure_future(
                            self._run_gated_node(
                                run_id,
                                node_key,
                                node,
                                user_request,
                                briefs,
                                completed,
                                failed_error_codes,
                                execution_iterations[node_key],
                                input,
                                inherited_source_ids,
                                previous_iteration,
                            )
                        )
                        pending.remove(node_key)
                        continue

                    current_phase = node_key
                    try:
                        await _activity(
                            "authorize_workflow_step", (run_id, "node", node_key)
                        )
                    except (ActivityError, ApplicationError) as exc:
                        # A pre-launch authorization failure (e.g. a grant
                        # revoked between run creation and this node's turn)
                        # must go through the same handled/unhandled decision
                        # as an execution failure below — otherwise a node
                        # with an on_failure fallback still unconditionally
                        # aborted the run whenever it failed *before*
                        # start_child_workflow rather than after.
                        error_code = _stable_error_code(exc)
                        if node_key not in nodes_with_failure_handler:
                            raise
                        await self._persist(
                            run_id, node_key, "failed", error_code=error_code
                        )
                        await self._publish_node_status(
                            node_key, "failed", error_code
                        )
                        outcomes[node_key] = "failed"
                        failed_error_codes[node_key] = error_code
                        pending.remove(node_key)
                        current_phase = None
                        progressed = True
                        await self._finalize_node_outcome(
                            run_id,
                            node_key,
                            "failed",
                            loop_edges,
                            loop_iterations,
                            previous_loop_iterations,
                            pending,
                            outcomes,
                            completed,
                            completed_source_ids,
                            failed_error_codes,
                            output_sizes,
                            input.web_tools_enabled,
                        )
                        continue
                    await self._persist(run_id, node_key, "running")
                    await self._publish_node_status(node_key, "running")
                    predecessor_outputs = _build_predecessor_outputs(
                        node, completed, failed_error_codes
                    )
                    handle = await workflow.start_child_workflow(
                        "AgentWorkflow",
                        _node_agent_input(
                            input,
                            node_key,
                            node,
                            user_request,
                            briefs[node_key],
                            predecessor_outputs,
                            previous_iteration,
                            self._turn_id,
                            execution_iterations[node_key],
                            inherited_source_ids,
                            self._produced_images,
                        ),
                        id=f"{run_id}:node:{node_key}",
                    )
                    self._active[node_key] = handle
                    pending.remove(node_key)
                    current_phase = None

                if not self._active:
                    # Nothing running and nothing launched this pass. If
                    # something is still pending *and* this pass resolved
                    # nothing at all (no skip, no handled authorize-failure),
                    # no path through the graph can ever make progress again
                    # — a genuine deadlock (the pre-existing guard for a
                    # malformed snapshot). If this pass did resolve something
                    # (via skip or a handled pre-launch failure) or pending is
                    # now empty, that's not a deadlock, just an empty pass —
                    # more nodes may become ready once the next pass's
                    # `_resolve_ready_and_skips` picks up this pass's new
                    # outcomes, or (if pending is empty too) the loop
                    # condition below ends the loop normally so the post-loop
                    # `not completed` check decides the outcome, instead of
                    # failing a run that may have already produced valid
                    # output on another, unrelated branch.
                    if pending and not progressed:
                        return await self._fail(run_id, "invalid_workflow_snapshot")
                    continue

                await workflow.wait(
                    tuple(self._active.values()), return_when=asyncio.FIRST_COMPLETED
                )
                finished_keys = sorted(
                    node_key
                    for node_key, handle in self._active.items()
                    if handle.done()
                )
                for node_key in finished_keys:
                    handle = self._active.pop(node_key)
                    try:
                        raw_output = await handle
                    except asyncio.CancelledError:
                        return await self._fail(run_id, "execution_failed", node_key)
                    except Exception as exc:
                        # A failure with no on_failure successor is unhandled and
                        # aborts the whole run exactly as before this feature —
                        # same single _fail call, same activity sequence, so a
                        # graph with zero on_failure edges replays byte-identically
                        # to pre-branching history.
                        error_code = _stable_error_code(exc)
                        if node_key not in nodes_with_failure_handler:
                            return await self._fail(run_id, error_code, node_key)
                        await self._persist(
                            run_id, node_key, "failed", error_code=error_code
                        )
                        await self._publish_node_status(
                            node_key, "failed", error_code
                        )
                        outcomes[node_key] = "failed"
                        failed_error_codes[node_key] = error_code
                        await self._finalize_node_outcome(
                            run_id,
                            node_key,
                            "failed",
                            loop_edges,
                            loop_iterations,
                            previous_loop_iterations,
                            pending,
                            outcomes,
                            completed,
                            completed_source_ids,
                            failed_error_codes,
                            output_sizes,
                            input.web_tools_enabled,
                        )
                        continue
                    node_result = _agent_result(raw_output)
                    output = node_result.answer
                    _check_output_size(node_key, output, output_sizes)
                    await self._persist(run_id, node_key, "completed", output)
                    await self._publish_node_status(node_key, "completed")
                    completed[node_key] = output
                    completed_source_ids[node_key] = node_result.source_ids
                    self._node_image_ids[node_key] = node_result.image_ids
                    self._produced_images.extend(node_result.image_ids)
                    outcomes[node_key] = "completed"
                    await self._finalize_node_outcome(
                        run_id,
                        node_key,
                        "completed",
                        loop_edges,
                        loop_iterations,
                        previous_loop_iterations,
                        pending,
                        outcomes,
                        completed,
                        completed_source_ids,
                        failed_error_codes,
                        output_sizes,
                        input.web_tools_enabled,
                    )

            if not completed:
                # Every node resolved to skipped or handled-failure — nothing
                # for the supervisor to synthesize from. Reachable only via
                # the `continue` above once both pending and self._active
                # have drained to empty; that same drain also covers a run
                # where something else already succeeded, which is why this
                # check — not the guard above — is what decides the outcome.
                return await self._fail(run_id, "invalid_workflow_snapshot")

            await _activity("transition_workflow_run", (run_id, "synthesizing", None))
            await self._publish_run_status("synthesizing")
            current_phase = "supervisor_synthesize"
            await _activity(
                "authorize_workflow_step", (run_id, "supervisor_synthesize", None)
            )
            await self._persist(run_id, current_phase, "running")
            context_used = tuple(
                {
                    "node_key": node_key,
                    "output": completed[node_key],
                    **(
                        {
                            "source_ids": list(
                                completed_source_ids.get(node_key, ())
                            )
                        }
                        if input.web_tools_enabled
                        else {}
                    ),
                }
                for node_key in sorted(completed)
            )
            inherited_source_ids = tuple(
                dict.fromkeys(
                    [
                        *preparation_result.source_ids,
                        *(
                            source_id
                            for node_key in sorted(completed_source_ids)
                            for source_id in completed_source_ids[node_key]
                        ),
                    ]
                )
            )
            synthesis_handle = await workflow.start_child_workflow(
                "AgentWorkflow",
                AgentInput(
                    agent=supervisor,
                    goal="Synthesize the final answer to the original user request.",
                    context={
                        "user_request": user_request,
                        "node_outputs": list(context_used),
                        **(
                            {
                                "preparation_source_ids": list(
                                    preparation_result.source_ids
                                )
                            }
                            if input.web_tools_enabled
                            else {}
                        ),
                    },
                    event_visibility="internal",
                    workflow_run_id=run_id,
                    workflow_phase="supervisor_synthesize",
                    turn_id=self._turn_id,
                    user_questions_enabled=input.user_questions_enabled,
                    web_tools_enabled=input.web_tools_enabled,
                    inherited_source_ids=inherited_source_ids,
                    image_ids=input.image_ids,
                ),
                id=f"{run_id}:supervisor_synthesize",
            )
            self._active[current_phase] = synthesis_handle
            synthesis_result = _agent_result(await synthesis_handle)
            answer = synthesis_result.answer
            self._active.pop(current_phase, None)
            _check_output_size("supervisor_synthesize", answer, output_sizes)
            await self._persist(run_id, current_phase, "completed", answer)
            current_phase = None
            await _activity("transition_workflow_run", (run_id, "completed", None))
            return MultiAgentResult(
                status="completed",
                answer=answer,
                model=supervisor.model,
                context_used=context_used,
                source_ids=synthesis_result.source_ids,
                unsupported_source_ids=synthesis_result.unsupported_source_ids,
                # Images of the nodes whose outputs the answer is built from.
                image_ids=tuple(
                    dict.fromkeys(
                        image_id
                        for node_key in sorted(completed)
                        for image_id in self._node_image_ids.get(node_key, ())
                    )
                ),
            )
        except asyncio.CancelledError:
            cancelled_keys = await self._cancel_active(run_id)
            if current_phase is not None and current_phase not in cancelled_keys:
                try:
                    await self._persist(run_id, current_phase, "cancelled")
                except Exception:
                    workflow.logger.warning(
                        f"Could not persist cancellation for {current_phase}"
                    )
            try:
                await _activity("transition_workflow_run", (run_id, "cancelled", None))
            except Exception:
                workflow.logger.warning("Could not persist cancelled workflow transition")
            return MultiAgentResult(status="cancelled")
        except (ActivityError, ChildWorkflowError, ApplicationError, ValueError) as exc:
            return await self._fail(run_id, _stable_error_code(exc), current_phase)
        except Exception as exc:
            workflow.logger.error(f"Unexpected multi-agent failure: {exc}")
            return await self._fail(run_id, "execution_failed", current_phase)
