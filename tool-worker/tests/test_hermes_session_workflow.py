import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from typing import Any, AsyncIterator

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from src import hermes_session_workflow

from src.hermes_session_workflow import (
    HEARTBEAT_THROTTLE,
    ANSWER_SIGNAL,
    CANCEL_SIGNAL,
    HERMES_SESSION_WORKFLOW,
    RESULT_SIGNAL,
    TURN_SIGNAL,
    HermesSessionInput,
    HermesSessionWorkflow,
    HermesTurn,
)

TOOLS = "tools-test"
RUNNER = "session-test"
SESSION = "a" * 32


@workflow.defn(name="FakeConversation")
class FakeConversation:
    """Stands in for ConversationWorkflow: collects the session's answers."""

    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    @workflow.signal(name=RESULT_SIGNAL)
    def result(self, result: dict[str, Any]) -> None:
        self.results.append(result)

    @workflow.query
    def answers(self) -> list[dict[str, Any]]:
        return self.results

    @workflow.run
    async def run(self, expected: int) -> list[dict[str, Any]]:
        await workflow.wait_condition(lambda: len(self.results) >= expected)
        return self.results


class Fakes:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.run_errors: list[str] = []
        self.prepare_error: str | None = None
        self.block_run = False
        self.run_cancelled = False
        self.answered: list[tuple[Any, ...]] = []

    def activities(self) -> list[Any]:
        @activity.defn(name="get_session_token")
        async def get_session_token() -> str:
            return "runner-token"

        @activity.defn(name="hermes_prepare_sandbox")
        async def prepare(token: str, org_id: str, conversation_id: str, model: str, turn_id: str) -> str:
            self.calls.append(f"prepare:{token}:{model}")
            if self.prepare_error:
                raise ApplicationError(self.prepare_error, type=self.prepare_error, non_retryable=True)
            return SESSION

        @activity.defn(name="hermes_bind_turn")
        async def bind(token: str, session_id: str, turn_id: str, execution_id: str) -> None:
            self.calls.append(f"bind:{turn_id}:{execution_id}")

        @activity.defn(name="hermes_unbind_turn")
        async def unbind(token: str, session_id: str) -> None:
            self.calls.append("unbind")

        @activity.defn(name="hermes_run_turn")
        async def run(
            session_id: str,
            turn_id: str,
            conversation_id: str,
            text: str,
            history: list[Any],
            org_id: str,
            parent_workflow_id: str,
        ) -> dict[str, Any]:
            self.calls.append(f"run:{turn_id}:{text}:{len(history)}")
            assert org_id == "org-1"
            if self.run_errors:
                code = self.run_errors.pop(0)
                raise ApplicationError(code, type=code, non_retryable=True)
            if self.block_run:
                try:
                    while self.block_run:
                        activity.heartbeat()
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    self.run_cancelled = True
                    raise
            return {"status": "completed", "output": f"answer to {text}"}

        @activity.defn(name="hermes_answer_question")
        async def answer(session_id: str, run_id: str, prompt_id: str, generation: int, text: str) -> str:
            self.answered.append((session_id, run_id, prompt_id, generation, text))
            self.block_run = False
            return "answered"

        @activity.defn(name="hermes_load_memory")
        async def load_memory(token: str, session_id: str) -> dict[str, Any]:
            self.calls.append("load_memory")
            return {"memory": {"version": 1, "sha256": "a"}, "user": {"version": 0, "sha256": "b"}}

        @activity.defn(name="hermes_save_memory")
        async def save_memory(token: str, session_id: str, loaded: dict[str, Any]) -> dict[str, str]:
            self.calls.append(f"save_memory:{loaded['memory']['version']}")
            return {"memory": "stored", "user": "unchanged"}

        @activity.defn(name="hermes_retire_sandbox")
        async def retire(session_id: str) -> None:
            self.calls.append(f"retire:{session_id}")

        @activity.defn(name="hermes_close_session")
        async def close(token: str, session_id: str) -> None:
            self.calls.append(f"close:{session_id}")

        return [prepare, bind, unbind, run, answer, load_memory, save_memory, retire, close], [get_session_token]


@asynccontextmanager
async def _stack(env: WorkflowEnvironment, fakes: Fakes) -> AsyncIterator[Client]:
    tool_activities, runner_activities = fakes.activities()
    async with (
        Worker(
            env.client,
            task_queue=TOOLS,
            workflows=[HermesSessionWorkflow],
            activities=tool_activities,
            max_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
        ),
        Worker(env.client, task_queue=RUNNER, workflows=[FakeConversation], activities=runner_activities),
    ):
        yield env.client


def _turn(parent: str, turn_id: str = "turn-1", **overrides: Any) -> dict[str, Any]:
    return asdict(
        HermesTurn(
            turn_id=turn_id,
            execution_id="exec-1",
            model="hermes-model",
            text=overrides.pop("text", "hello"),
            history=[{"role": "user", "content": "earlier"}],
            parent_workflow_id=parent,
            parent_task_queue=RUNNER,
            **overrides,
        )
    )


async def _hand_off(client: Client, parent: str, turn: dict[str, Any], *, idle: int = 1800) -> Any:
    return await client.start_workflow(
        HERMES_SESSION_WORKFLOW,
        asdict(HermesSessionInput(org_id="org-1", conversation_id="conv-1", idle_seconds=idle)),
        id="hermes-session-conv-1",
        task_queue=TOOLS,
        start_signal=TURN_SIGNAL,
        start_signal_args=[turn],
    )


async def _parent(client: Client, expected: int = 1) -> tuple[str, Any]:
    parent = f"conversation-{uuid.uuid4()}"
    handle = await client.start_workflow("FakeConversation", expected, id=parent, task_queue=RUNNER)
    return parent, handle


@pytest.mark.asyncio
async def test_a_turn_is_prepared_bound_run_and_unbound() -> None:
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client)
        await _hand_off(client, parent, _turn(parent))

        [result] = await handle.result()

    assert result == {"turn_id": "turn-1", "status": "completed", "output": "answer to hello"}
    # The memory goes in after binding and comes back before unbinding.
    assert fakes.calls == [
        "prepare:runner-token:hermes-model",
        "bind:turn-1:exec-1",
        "load_memory",
        "run:turn-1:hello:1",
        "save_memory:1",
        "unbind",
    ]


@pytest.mark.asyncio
async def test_turns_run_one_after_another_in_one_session() -> None:
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client, expected=2)
        session = await _hand_off(client, parent, _turn(parent))
        await session.signal(TURN_SIGNAL, _turn(parent, "turn-2", text="again"))
        # A repeated hand-off of the same turn is not run twice.
        await session.signal(TURN_SIGNAL, _turn(parent, "turn-2", text="again"))

        results = await handle.result()

    assert [r["turn_id"] for r in results] == ["turn-1", "turn-2"]
    assert [call for call in fakes.calls if call.startswith("run")] == ["run:turn-1:hello:1", "run:turn-2:again:1"]


@pytest.mark.asyncio
async def test_a_cancelled_turn_stops_its_run_and_still_unbinds() -> None:
    fakes = Fakes()
    fakes.block_run = True
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client)
        session = await _hand_off(client, parent, _turn(parent))
        for _ in range(100):
            if any(call.startswith("run") for call in fakes.calls):
                break
            await asyncio.sleep(0.05)
        await session.signal(CANCEL_SIGNAL, "turn-1")

        [result] = await handle.result()

    assert result == {"turn_id": "turn-1", "status": "cancelled"}
    assert fakes.run_cancelled
    assert fakes.calls[-1] == "unbind"


@pytest.mark.asyncio
async def test_a_sandbox_retired_before_the_run_is_prepared_again_once() -> None:
    fakes = Fakes()
    fakes.run_errors = ["hermes_sandbox_not_found"]
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client)
        await _hand_off(client, parent, _turn(parent))

        [result] = await handle.result()

    assert result["status"] == "completed"
    assert [call.split(":")[0] for call in fakes.calls] == [
        "prepare", "bind", "load_memory", "run", "prepare", "bind", "load_memory", "run", "save_memory", "unbind",
    ]


@pytest.mark.asyncio
async def test_a_failure_is_reported_with_its_code() -> None:
    fakes = Fakes()
    fakes.prepare_error = "hermes_capacity_exhausted"
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client)
        await _hand_off(client, parent, _turn(parent))

        [result] = await handle.result()

    assert result == {"turn_id": "turn-1", "status": "failed", "error": "hermes_capacity_exhausted"}
    assert "unbind" not in fakes.calls


@pytest.mark.asyncio
async def test_an_idle_session_retires_its_sandbox_closes_and_ends() -> None:
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client)
        session = await _hand_off(client, parent, _turn(parent), idle=600)
        await handle.result()

        await env.sleep(timedelta(seconds=700))
        await session.result()

    assert fakes.calls[-2:] == [f"retire:{SESSION}", f"close:{SESSION}"]


@pytest.mark.asyncio
async def test_without_a_runner_to_ask_for_a_token_the_turn_fails_instead_of_hanging(monkeypatch) -> None:
    # The test server does not skip time while an activity task waits.
    monkeypatch.setattr(hermes_session_workflow, "_TOKEN_WAIT", timedelta(seconds=2))
    fakes = Fakes()
    tool_activities, _ = fakes.activities()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            Worker(
                env.client,
                task_queue=TOOLS,
                workflows=[HermesSessionWorkflow],
                activities=tool_activities,
                # Unsandboxed, so the shortened wait applies.
                workflow_runner=UnsandboxedWorkflowRunner(),
            ),
            # The conversation's workflow runs, but no worker serves its runner activities.
            Worker(env.client, task_queue="answers-only", workflows=[FakeConversation]),
        ):
            parent = f"conversation-{uuid.uuid4()}"
            handle = await env.client.start_workflow("FakeConversation", 1, id=parent, task_queue="answers-only")
            await _hand_off(env.client, parent, _turn(parent))

            [result] = await handle.result()

    assert result["status"] == "failed"
    assert fakes.calls == []


@pytest.mark.asyncio
async def test_a_turn_handed_off_again_after_its_answer_is_not_run_twice() -> None:
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client, expected=2)
        session = await _hand_off(client, parent, _turn(parent))
        for _ in range(200):
            if (await handle.query("answers")):
                break
            await asyncio.sleep(0.05)
        await session.signal(TURN_SIGNAL, _turn(parent))
        await session.signal(TURN_SIGNAL, _turn(parent, "turn-2"))

        results = await handle.result()

    assert [r["turn_id"] for r in results] == ["turn-1", "turn-2"]
    assert [call for call in fakes.calls if call.startswith("run")] == ["run:turn-1:hello:1", "run:turn-2:hello:1"]


@pytest.mark.asyncio
async def test_a_vanished_conversation_does_not_end_the_session() -> None:
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        session = await _hand_off(client, "conversation-gone", _turn("conversation-gone"))
        parent, handle = await _parent(client)
        await session.signal(TURN_SIGNAL, _turn(parent, "turn-2"))

        [result] = await handle.result()

    assert result["turn_id"] == "turn-2" and result["status"] == "completed"


@pytest.mark.asyncio
async def test_an_answer_reaches_the_running_turn_and_others_are_ignored() -> None:
    fakes = Fakes()
    fakes.block_run = True
    async with await WorkflowEnvironment.start_time_skipping() as env, _stack(env, fakes) as client:
        parent, handle = await _parent(client)
        session = await _hand_off(client, parent, _turn(parent))
        for _ in range(100):
            if any(call.startswith("run") for call in fakes.calls):
                break
            await asyncio.sleep(0.05)
        answer = {"run_id": "run_1", "prompt_id": "p1", "execution_generation": 1, "answer": "Rome"}
        await session.signal(ANSWER_SIGNAL, {**answer, "turn_id": "another-turn"})
        await session.signal(ANSWER_SIGNAL, {**answer, "turn_id": "turn-1"})

        [result] = await handle.result()

    assert result["status"] == "completed"
    assert fakes.answered == [(SESSION, "run_1", "p1", 1, "Rome")]
