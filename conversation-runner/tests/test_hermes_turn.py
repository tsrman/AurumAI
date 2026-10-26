"""A Hermes Agent turn in ConversationWorkflow: registered as the turn's
execution, handed to the session workflow, answered by its result signal."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.config import settings
from src.workflows.builtin_tools import ExecutionContext
from src.workflows.conversation import ConversationInput, ConversationWorkflow
from tests.test_conversation_tool_loop import (
    _make_persist_assistant_message,
    _make_publish_event,
    mock_load_conversation_state,
    mock_record_langfuse_score,
    mock_record_message_score,
)

TURN = "11111111-1111-4111-8111-111111111111"
EXECUTION = "22222222-2222-4222-8222-222222222222"


class Session:
    """Stands in for tool-worker's HermesSessionWorkflow."""

    def __init__(self, result: dict[str, Any] | None) -> None:
        self.result = result
        self.turns: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.touched: list[str] = []

    def activities(self) -> list[Any]:
        @activity.defn(name="hand_off_hermes_turn")
        async def hand_off(turn: dict[str, Any]) -> None:
            self.turns.append(turn)
            if self.result is not None:
                handle = activity.client().get_workflow_handle(turn["parent_workflow_id"])
                await handle.signal("hermes_turn_result", {"turn_id": turn["turn_id"], **self.result})

        @activity.defn(name="cancel_hermes_turn")
        async def cancel(turn_id: str) -> None:
            self.cancelled.append(turn_id)

        @activity.defn(name="register_interaction_execution")
        async def register(*args: Any) -> ExecutionContext:
            return ExecutionContext(turn_id=str(args[0]), execution_id=EXECUTION, phase="ordinary", node_key=None)

        @activity.defn(name="touch_hermes_turn")
        async def touch(turn_id: str) -> None:
            self.touched.append(turn_id)

        @activity.defn(name="fail_interaction_turn")
        async def fail(turn_id: str) -> None:
            self.cancelled.append(f"failed:{turn_id}")

        return [hand_off, cancel, register, fail, touch]


async def _turn(
    session: Session,
    message: dict[str, Any],
    *,
    cancel_after: float | None = None,
    answer_after: timedelta | None = None,
) -> tuple[Any, ...]:
    persist, persisted = _make_persist_assistant_message()
    publish, events = _make_publish_event()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-tq",
            workflows=[ConversationWorkflow],
            activities=[
                mock_load_conversation_state,
                persist,
                publish,
                mock_record_langfuse_score,
                mock_record_message_score,
                *session.activities(),
            ],
        ):
            handle = await env.client.start_workflow(
                ConversationWorkflow.run,
                ConversationInput(conversation_id="conv-hermes"),
                id="conversation-conv-hermes",
                task_queue="test-tq",
            )
            await handle.signal(ConversationWorkflow.user_message, message)
            if cancel_after is not None:
                for _ in range(200):
                    if session.turns:
                        break
                    await asyncio.sleep(0.05)
                await handle.signal(ConversationWorkflow.cancel_turn, TURN)
            if answer_after is not None:
                await env.sleep(answer_after)
                await handle.signal("hermes_turn_result", {"turn_id": TURN, "status": "completed", "output": "late"})
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            await handle.result()
            history = await handle.fetch_history()
    return persisted, events, history


def _message(**extra: Any) -> dict[str, Any]:
    return {"role": "user", "content": "plan a trip", "turn_id": TURN, "model": "hermes-model", "hermes": True, **extra}


@pytest.mark.asyncio
async def test_a_hermes_turn_is_handed_over_and_its_answer_persisted() -> None:
    session = Session({"status": "completed", "output": "Here is the plan."})

    persisted, events, history = await _turn(session, _message())

    [turn] = session.turns
    assert turn["turn_id"] == TURN and turn["execution_id"] == EXECUTION
    assert turn["model"] == "hermes-model" and turn["text"] == "plan a trip"
    assert turn["parent_workflow_id"] == "conversation-conv-hermes" and turn["parent_task_queue"] == "test-tq"
    assert turn["history"] == []
    assert persisted == [
        {"content": "Here is the plan.", "model": "hermes-model", "turn_id": TURN, "tool_calls": None, "tool_results": None}
    ]
    assert [name for name, _ in events] == ["message-start", "message-end", "done"]


@pytest.mark.asyncio
async def test_a_failed_hermes_turn_ends_with_a_reason_and_no_answer() -> None:
    session = Session({"status": "failed", "error": "hermes_capacity_exhausted"})

    persisted, events, _ = await _turn(session, _message())

    assert persisted == []
    assert f"failed:{TURN}" in session.cancelled
    message_end = next(data for name, data in events if name == "message-end")
    assert "hermes_capacity_exhausted" in message_end["content"]
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_cancelling_a_hermes_turn_cancels_it_in_the_session() -> None:
    session = Session(None)

    persisted, events, _ = await _turn(session, _message(), cancel_after=0)

    assert session.cancelled == [TURN]
    assert persisted == []
    assert "message-end" not in [name for name, _ in events]


@pytest.mark.asyncio
async def test_a_hermes_turn_records_the_hermes_marker() -> None:
    # Only Hermes turns evaluate the patch, so earlier histories replay as
    # they were recorded (test_interaction_replay covers those).
    session = Session({"status": "completed", "output": "x"})

    _, _, history = await _turn(session, _message())

    patches = [
        payload.data
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        for payloads in event.marker_recorded_event_attributes.details.values()
        for payload in payloads.payloads
    ]
    assert any(b"hermes-v1" in data for data in patches)


@pytest.mark.asyncio
async def test_a_long_turn_keeps_its_runner_until_the_answer() -> None:
    session = Session(None)

    persisted, _, _ = await _turn(session, _message(), answer_after=timedelta(seconds=1000))

    assert session.touched == [TURN, TURN, TURN]
    assert [item["content"] for item in persisted] == ["late"]


@pytest.mark.asyncio
async def test_a_session_that_never_answers_is_cancelled_and_the_turn_fails(monkeypatch) -> None:
    monkeypatch.setattr(settings, "hermes_turn_deadline_seconds", 600)
    session = Session(None)

    persisted, events, _ = await _turn(session, _message())

    assert session.cancelled[0] == TURN and f"failed:{TURN}" in session.cancelled
    assert persisted == []
    assert "hermes_turn_timeout" in next(data for name, data in events if name == "message-end")["content"]
