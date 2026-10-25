"""Image tools and attachments inside workflow node agents."""

import asyncio
import json
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from src.workflows.agent import AgentExecutionSnapshot, AgentInput, AgentResult, AgentWorkflow
from src.workflows.builtin_tools import ExecutionContext
from src.workflows.multi_agent import MultiAgentInput, MultiAgentWorkflow, _agent_result

GENERATED = "11111111-1111-4111-8111-111111111111"
EDITED = "22222222-2222-4222-8222-222222222222"
ATTACHED = "33333333-3333-4333-8333-333333333333"
EARLIER = "44444444-4444-4444-8444-444444444444"
TURN = "55555555-5555-4555-8555-555555555555"
RUN = "66666666-6666-4666-8666-666666666666"


def _agent(name: str = "Artist") -> AgentExecutionSnapshot:
    return AgentExecutionSnapshot(
        agent_definition_id=f"agent-{name}",
        name=name,
        model="test-model",
        system_prompt="Make pictures.",
        temperature=0.0,
    )


def _node_input(**overrides: Any) -> AgentInput:
    values: dict[str, Any] = dict(
        agent=_agent(),
        goal="Draw a fox, then make it blue",
        event_visibility="internal",
        workflow_run_id=RUN,
        workflow_phase="node",
        workflow_node_key="draw",
        turn_id=TURN,
    )
    values.update(overrides)
    return AgentInput(**values)


async def _run_agent(
    input: AgentInput,
    responses: list[str],
    image_results: list[dict[str, Any] | str],
) -> tuple[Any, list[dict[str, Any]], list[tuple[Any, ...]], Any]:
    requests: list[dict[str, Any]] = []
    calls: list[tuple[Any, ...]] = []

    @activity.defn(name="list_available_skills")
    async def list_available_skills() -> list[dict[str, Any]]:
        return []

    @activity.defn(name="get_conversation_id")
    async def get_conversation_id() -> str:
        return "conversation-images"

    @activity.defn(name="get_session_token")
    async def get_session_token() -> str:
        return "session-token"

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict[str, Any], turn_id: str | None = None, publish_events: bool = True
    ) -> str:
        del turn_id, publish_events
        requests.append(request)
        return responses[len(requests) - 1]

    @activity.defn(name="register_interaction_execution")
    async def register_interaction_execution(*args: Any) -> ExecutionContext:
        return ExecutionContext(
            turn_id=str(args[0]),
            execution_id="77777777-7777-4777-8777-777777777777",
            phase="node",
            node_key="draw",
        )

    @activity.defn(name="execute_image_tool")
    async def execute_image_tool(*args: Any) -> dict[str, Any]:
        calls.append(args[1:])
        result = image_results[len(calls) - 1]
        if isinstance(result, str):
            raise ApplicationError(result, type=result, non_retryable=True)
        return result

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="workflow-images-agent",
            workflows=[AgentWorkflow],
            activities=[
                list_available_skills,
                get_conversation_id,
                get_session_token,
                llm_call,
                register_interaction_execution,
                execute_image_tool,
            ],
        ):
            handle = await env.client.start_workflow(
                AgentWorkflow.run, input, id="images-agent", task_queue="workflow-images-agent"
            )
            result = await handle.result()
            history = await handle.fetch_history()
    return result, requests, calls, history


def _image_result(tool: str, image_id: str) -> dict[str, Any]:
    return {
        "kind": "image_result",
        "tool": tool,
        "image_ids": [image_id],
        "text": json.dumps({"imageId": image_id, "contentType": "image/png"}),
    }


@pytest.mark.asyncio
async def test_a_node_agent_generates_and_edits_and_reports_its_images() -> None:
    responses = [
        '<tool_call>{"tool":"generate_image","args":{"prompt":"a red fox"}}</tool_call>',
        '<tool_call>{"tool":"edit_image","args":{"image_id":"'
        + GENERATED
        + '","prompt":"make it blue"}}</tool_call>',
        f"A blue fox (imageId {EDITED}).",
    ]
    result, requests, calls, history = await _run_agent(
        _node_input(
            image_generation_enabled=True,
            image_editing_enabled=True,
            recent_images=({"id": EARLIER, "prompt": "a cat", "origin": "uploaded"},),
            image_ids=(ATTACHED,),
        ),
        responses,
        [_image_result("generate_image", GENERATED), _image_result("edit_image", EDITED)],
    )

    assert isinstance(result, AgentResult)
    assert result.image_ids == (GENERATED, EDITED)
    assert [call[1] for call in calls] == ["generate_image", "edit_image"]
    system = requests[0]["messages"][0]["content"]
    assert "generate_image" in system and "edit_image" in system
    assert EARLIER in system and "attached by the user" in system
    # The attachment rides on the goal message, for the proxy to inline.
    assert requests[0]["messages"][1]["image_ids"] == [ATTACHED]
    assert "Tool result for edit_image" in requests[2]["messages"][-1]["content"]
    await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["image_quota_exceeded", "image_cancelled", "image_rejected"])
async def test_image_errors_become_tool_results_for_the_agent(code: str) -> None:
    result, requests, _, _ = await _run_agent(
        _node_input(image_generation_enabled=True),
        [
            '<tool_call>{"tool":"generate_image","args":{"prompt":"a fox"}}</tool_call>',
            "I could not create the image.",
        ],
        [code],
    )

    assert isinstance(result, AgentResult) and result.image_ids == ()
    assert code in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_an_agent_without_images_keeps_its_old_shape_and_records_no_marker() -> None:
    result, requests, _, history = await _run_agent(_node_input(), ["Done."], [])

    assert result == "Done."
    assert "image" not in requests[0]["messages"][0]["content"]
    assert "image_ids" not in requests[0]["messages"][1]
    assert "image-tools-v1" not in json.dumps(history.to_json_dict())


def test_agent_results_carry_image_ids_strictly() -> None:
    assert _agent_result({"answer": "x", "imageIds": [GENERATED]}).image_ids == (GENERATED,)
    assert _agent_result("plain").image_ids == ()
    with pytest.raises(ValueError, match="invalid_agent_result"):
        _agent_result({"answer": "x", "image_ids": [7]})


SEEN: list[AgentInput] = []


@workflow.defn(name="AgentWorkflow")
class ImageNodeAgentStub:
    @workflow.run
    async def run(self, input: AgentInput) -> str | AgentResult:
        SEEN.append(input)
        if input.workflow_phase == "supervisor_prepare":
            return '{"draw":"draw it","paint":"paint it"}'
        if input.workflow_phase == "node" and input.workflow_node_key == "draw":
            return AgentResult(answer=f"drew {GENERATED}", image_ids=(GENERATED,))
        if input.workflow_phase == "node":
            return AgentResult(answer=f"painted {EDITED}", image_ids=(EDITED,))
        return "Here is your blue fox."


def _snapshot() -> dict[str, Any]:
    agent = {
        "agentDefinitionId": "artist",
        "name": "Artist",
        "model": "test-model",
        "systemPrompt": "Draw.",
        "temperature": 0.0,
        "allowedSkillIds": [],
        "knowledgeBases": [],
    }
    return {
        "runId": RUN,
        "turnId": TURN,
        "userRequest": "A blue fox",
        "maxParallelism": 1,
        "supervisor": {**agent, "agentDefinitionId": "supervisor", "name": "Supervisor"},
        "nodes": [
            {"nodeKey": "draw", "displayName": "Draw", "instruction": "Draw.", "agent": agent},
            {
                "nodeKey": "paint",
                "displayName": "Paint",
                "instruction": "Edit the drawing.",
                "agent": agent,
                # The confirmation-gated path builds its agent input separately.
                "requiresConfirmation": True,
            },
        ],
        "edges": [{"sourceNodeKey": "draw", "targetNodeKey": "paint"}],
    }


@pytest.mark.asyncio
async def test_workflow_nodes_get_images_and_the_final_answer_lists_them() -> None:
    SEEN.clear()
    snapshot = _snapshot()
    waiting = asyncio.Event()

    @activity.defn(name="load_workflow_run_snapshot")
    async def load_workflow_run_snapshot(run_id: str) -> dict[str, Any]:
        return snapshot

    @activity.defn(name="authorize_workflow_step")
    async def authorize_workflow_step(*args: Any) -> None:
        del args

    @activity.defn(name="transition_workflow_run")
    async def transition_workflow_run(*args: Any) -> None:
        del args

    @activity.defn(name="persist_workflow_node_result")
    async def persist_workflow_node_result(*args: Any) -> None:
        if args[1] == "paint" and args[2] == "waiting_human":
            waiting.set()

    @activity.defn(name="publish_event")
    async def publish_event(*args: Any) -> None:
        del args

    @activity.defn(name="refresh_runner_activity")
    async def refresh_runner_activity(run_id: str) -> None:
        del run_id

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="workflow-images",
            workflows=[MultiAgentWorkflow, ImageNodeAgentStub],
            # The stub records its inputs in this module's list.
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                load_workflow_run_snapshot,
                authorize_workflow_step,
                transition_workflow_run,
                persist_workflow_node_result,
                publish_event,
                refresh_runner_activity,
            ],
        ):
            handle = await env.client.start_workflow(
                MultiAgentWorkflow.run,
                MultiAgentInput(
                    run_id=RUN,
                    image_generation_enabled=True,
                    image_editing_enabled=True,
                    recent_images=({"id": EARLIER, "prompt": "a cat", "origin": "uploaded"},),
                    image_ids=(ATTACHED,),
                ),
                id="workflow-images",
                task_queue="workflow-images",
            )
            await asyncio.wait_for(waiting.wait(), timeout=10)
            await handle.signal(MultiAgentWorkflow.submit_human_decision, args=["paint", "approve"])
            result = await handle.result()

    assert result.status == "completed"
    assert result.image_ids == (GENERATED, EDITED)
    by_phase = {(item.workflow_phase, item.workflow_node_key): item for item in SEEN}
    prepare = by_phase[("supervisor_prepare", None)]
    synthesize = by_phase[("supervisor_synthesize", None)]
    draw, paint = by_phase[("node", "draw")], by_phase[("node", "paint")]
    # Attachments reach every agent; image tools only the node agents.
    assert all(item.image_ids == (ATTACHED,) for item in (prepare, draw, paint, synthesize))
    assert not prepare.image_generation_enabled and not synthesize.image_editing_enabled
    assert draw.image_generation_enabled and paint.image_editing_enabled
    # The gated node sees the earlier node's image first, so it can edit it.
    assert [image["id"] for image in paint.recent_images] == [GENERATED, EARLIER]
    assert [image["id"] for image in draw.recent_images] == [EARLIER]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "code"),
    [
        ('{"tool":"generate_image","args":{"prompt":"a fox"}}', "unknown_tool"),
        ('{"tool":"teleport","args":{}}', "unknown_tool"),
    ],
)
async def test_an_unavailable_built_in_is_answered_instead_of_hanging(call: str, code: str) -> None:
    result, requests, calls, history = await _run_agent(
        _node_input(),
        [f"<tool_call>{call}</tool_call>", "Answer without that tool."],
        [],
    )

    assert result == "Answer without that tool." and calls == []
    assert requests[1]["messages"][-1]["content"].endswith(f": {code}")
    await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_malformed_arguments_for_an_offered_built_in_are_answered() -> None:
    result, requests, calls, _ = await _run_agent(
        _node_input(image_generation_enabled=True),
        [
            '<tool_call>{"tool":"generate_image","args":{"prompt":"   "}}</tool_call>',
            "I need a real prompt.",
        ],
        [],
    )

    assert isinstance(result, AgentResult) and calls == []
    assert requests[1]["messages"][-1]["content"] == "Tool result for generate_image: invalid_arguments"
