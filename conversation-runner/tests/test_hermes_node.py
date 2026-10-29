"""A Hermes Agent workflow node (docs/HERMES_AGENT.md, phase 6): AgentWorkflow
hands it to the conversation's Hermes session under its own key, with the
run's correlation, and waits for that key's answer."""

import asyncio
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows.agent import AgentExecutionSnapshot, AgentInput, AgentWorkflow
from src.workflows.builtin_tools import ExecutionContext

TURN = "11111111-1111-4111-8111-111111111111"
EXECUTION = "22222222-2222-4222-8222-222222222222"
KEY = f"{TURN}:{EXECUTION}"


def _input(**extra: Any) -> AgentInput:
    return AgentInput(
        agent=AgentExecutionSnapshot(
            agent_definition_id="agent-h",
            name="Builder",
            model="hermes-model",
            system_prompt="Build it in the workspace.",
            temperature=0.2,
            kind="hermes",
        ),
        goal="User request:\nship it\n\nNode instruction:\nwrite the script",
        context={"predecessor_outputs": [{"node_key": "research", "output": "use python"}]},
        event_visibility="internal",
        workflow_run_id="run-9",
        workflow_phase="node",
        workflow_node_key="build",
        turn_id=TURN,
        **extra,
    )


class Session:
    """Stands in for the runner's Hermes activities and tool-worker's session."""

    def __init__(self, results: list[dict[str, Any]] | None) -> None:
        self.results = results
        self.turns: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def activities(self) -> list[Any]:
        @activity.defn(name="register_interaction_execution")
        async def register(*args: Any) -> ExecutionContext:
            assert args[4:7] == ("node", "build", "agent-h")
            return ExecutionContext(turn_id=TURN, execution_id=EXECUTION, phase="node", node_key="build")

        @activity.defn(name="get_conversation_id")
        async def conversation_id() -> str:
            return "conv-1"

        @activity.defn(name="hand_off_hermes_turn")
        async def hand_off(turn: dict[str, Any]) -> None:
            self.turns.append(turn)
            handle = activity.client().get_workflow_handle(turn["parent_workflow_id"])
            for result in self.results or []:
                await handle.signal("hermes_turn_result", result)

        @activity.defn(name="cancel_hermes_turn")
        async def cancel(turn_id: str) -> None:
            self.cancelled.append(turn_id)

        @activity.defn(name="touch_hermes_turn")
        async def touch(turn_id: str) -> None:
            return None

        return [register, conversation_id, hand_off, cancel, touch]


async def _node(session: Session, *, cancel: bool = False) -> Any:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="test-tq", workflows=[AgentWorkflow], activities=session.activities()):
            handle = await env.client.start_workflow(AgentWorkflow.run, _input(), id="run-9:node:build", task_queue="test-tq")
            if cancel:
                for _ in range(200):
                    if session.turns:
                        break
                    await asyncio.sleep(0.05)
                await handle.cancel()
            try:
                return await handle.result()
            except WorkflowFailureError as exc:
                return exc


@pytest.mark.asyncio
async def test_a_hermes_node_is_handed_to_the_session_under_its_own_key() -> None:
    session = Session(
        [
            # A loop iteration's earlier key reaches this id too: ignored.
            {"turn_id": f"{TURN}:other", "status": "completed", "output": "stale"},
            {"turn_id": KEY, "status": "completed", "output": "Script written."},
        ]
    )

    answer = await _node(session)

    [turn] = session.turns
    assert answer == "Script written."
    assert (turn["turn_id"], turn["execution_id"], turn["run_key"]) == (TURN, EXECUTION, KEY)
    assert (turn["workflow_run_id"], turn["workflow_phase"], turn["workflow_node_key"]) == ("run-9", "node", "build")
    assert turn["publish_progress"] is False and turn["history"] == []
    assert turn["model"] == "hermes-model" and turn["parent_workflow_id"] == "run-9:node:build"
    assert turn["text"].startswith("Build it in the workspace.") and "write the script" in turn["text"]
    assert "use python" in turn["text"]


@pytest.mark.asyncio
async def test_a_failed_hermes_node_fails_with_the_sessions_code() -> None:
    session = Session([{"turn_id": KEY, "status": "failed", "error": "hermes_model_unavailable"}])

    failure = await _node(session)

    assert isinstance(failure, WorkflowFailureError)
    assert failure.cause.type == "hermes_model_unavailable"


@pytest.mark.asyncio
async def test_a_cancelled_node_stops_its_hermes_turn() -> None:
    session = Session(None)

    await _node(session, cancel=True)

    assert session.cancelled == [KEY]
