"""Questions Hermes asks during a turn: asked as the turn's question cards,
answered back to the session workflow, and answered "skip" when the user
cannot be asked or does not answer."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
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
QUESTION = "33333333-3333-4333-8333-333333333333"
PROMPT = {
    "turn_id": TURN,
    "run_id": "run_1",
    "prompt_id": "p1",
    "execution_generation": 1,
    "text": "Which city?",
    "choices": ["Paris", "Rome"],
    "multi_select": False,
}


@workflow.defn(name="FakeHermesSession")
class FakeHermesSession:
    """Takes the answer as tool-worker's session workflow would, and finishes the turn with it."""

    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    @workflow.signal(name="hermes_answer_question")
    def answer(self, answer: dict[str, Any]) -> None:
        self.answers.append(answer)

    @workflow.query
    def received(self) -> list[dict[str, Any]]:
        return self.answers

    @workflow.run
    async def run(self, parent: str) -> None:
        await workflow.wait_condition(lambda: bool(self.answers))
        await workflow.get_external_workflow_handle(parent).signal(
            "hermes_turn_result", {"turn_id": TURN, "status": "completed", "output": f"Going to {self.answers[0]['answer']}"}
        )


class Backend:
    def __init__(
        self,
        *,
        create_error: str | None = None,
        statuses: list[str] | None = None,
        answer: dict | None = None,
        expires_in: timedelta = timedelta(hours=1),
    ) -> None:
        self.create_error = create_error
        self.expires_in = expires_in
        self.statuses = statuses or ["answered"]
        self.answer = answer or {"optionId": "c1"}
        self.calls: list[str] = []
        self.created: list[Any] = []
        self.touched = 0

    def activities(self, prompts: list[dict[str, Any]]) -> list[Any]:
        @activity.defn(name="hand_off_hermes_turn")
        async def hand_off(turn: dict[str, Any]) -> None:
            handle = activity.client().get_workflow_handle(turn["parent_workflow_id"])
            for prompt in prompts:
                await handle.signal("hermes_question", prompt)

        @activity.defn(name="register_interaction_execution")
        async def register(*args: Any) -> ExecutionContext:
            return ExecutionContext(turn_id=str(args[0]), execution_id=EXECUTION, phase="ordinary", node_key=None)

        @activity.defn(name="create_user_question")
        async def create(context: ExecutionContext, invocation_id: str, question: dict[str, Any]) -> dict[str, Any]:
            self.calls.append("create")
            self.created.append((invocation_id, question))
            if self.create_error:
                raise ApplicationError(self.create_error, type=self.create_error, non_retryable=True)
            expires = datetime.now(timezone.utc) + self.expires_in
            return {"questionId": QUESTION, "expiresAt": expires.isoformat()}

        @activity.defn(name="read_user_question")
        async def read(question_id: str) -> dict[str, Any]:
            self.calls.append("read")
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"questionId": question_id, "status": status}

        @activity.defn(name="consume_user_question")
        async def consume(question_id: str) -> dict[str, Any]:
            self.calls.append("consume")
            return self.answer

        @activity.defn(name="expire_user_question")
        async def expire(question_id: str) -> dict[str, Any]:
            self.calls.append("expire")
            return {"questionId": question_id, "status": "expired"}

        @activity.defn(name="touch_hermes_turn")
        async def touch(turn_id: str) -> None:
            self.touched += 1

        @activity.defn(name="cancel_hermes_turn")
        async def cancel(turn_id: str) -> None:
            self.calls.append("cancel")

        @activity.defn(name="fail_interaction_turn")
        async def fail(turn_id: str) -> None:
            self.calls.append("fail")

        return [hand_off, register, create, read, consume, expire, touch, cancel, fail]


async def _turn(
    backend: Backend, prompts: list[dict[str, Any]], *, enabled: bool = True, settle_after: timedelta | None = None,
    answer_after: timedelta | None = None,
) -> tuple[list[dict], list[dict[str, Any]]]:
    persist, persisted = _make_persist_assistant_message()
    publish, _ = _make_publish_event()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-tq",
            workflows=[ConversationWorkflow, FakeHermesSession],
            activities=[
                mock_load_conversation_state,
                persist,
                publish,
                mock_record_langfuse_score,
                mock_record_message_score,
                *backend.activities(prompts),
            ],
        ):
            parent = "conversation-conv-hermes"
            session = await env.client.start_workflow(
                FakeHermesSession.run, parent, id="hermes-session-conv-hermes", task_queue="test-tq"
            )
            handle = await env.client.start_workflow(
                ConversationWorkflow.run, ConversationInput(conversation_id="conv-hermes"), id=parent, task_queue="test-tq"
            )
            await handle.signal(
                ConversationWorkflow.user_message,
                {"role": "user", "content": "plan a trip", "turn_id": TURN, "model": "m", "hermes": True,
                 "user_questions_enabled": enabled},
            )
            if settle_after is not None:
                await env.sleep(settle_after)
                await handle.signal("hermes_question_settled", {"turn_id": TURN, "prompt_id": "p1"})
                await env.sleep(timedelta(seconds=60))
                await handle.signal("hermes_turn_result", {"turn_id": TURN, "status": "completed", "output": "fine"})
            if answer_after is not None:
                await env.sleep(answer_after)
                backend.statuses = ["answered"]
                await handle.signal(ConversationWorkflow.question_available, args=[QUESTION, 2])
            await env.sleep(timedelta(seconds=settings.idle_ttl_seconds + 10))
            await handle.result()
            answers = await session.query(FakeHermesSession.received)
    return persisted, answers


@pytest.mark.asyncio
async def test_a_question_is_asked_as_a_card_and_its_choice_sent_back() -> None:
    backend = Backend()

    persisted, answers = await _turn(backend, [PROMPT, PROMPT])

    [(invocation, question)] = backend.created
    assert question == {"text": "Which city?", "options": [{"id": "c0", "label": "Paris"}, {"id": "c1", "label": "Rome"}]}
    assert len(invocation) == 36
    assert answers == [
        {"turn_id": TURN, "run_id": "run_1", "prompt_id": "p1", "execution_generation": 1, "answer": "Rome"}
    ]
    assert [item["content"] for item in persisted] == ["Going to Rome"]


@pytest.mark.asyncio
async def test_a_typed_answer_is_sent_as_written() -> None:
    backend = Backend(answer={"text": "Lisbon, actually"})

    _, answers = await _turn(backend, [PROMPT])

    assert answers[0]["answer"] == "Lisbon, actually"


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "error"), [(False, None), (True, "user_input_limit"), (True, "user_input_disabled")])
async def test_a_question_that_cannot_be_asked_is_skipped(enabled: bool, error: str | None) -> None:
    backend = Backend(create_error=error)

    _, answers = await _turn(backend, [PROMPT], enabled=enabled)

    assert answers[0]["answer"] == "skip"
    assert ("create" in backend.calls) is enabled


@pytest.mark.asyncio
async def test_an_unanswered_question_expires_at_its_deadline_and_is_skipped() -> None:
    backend = Backend(statuses=["pending"], expires_in=timedelta(minutes=10))

    _, answers = await _turn(backend, [PROMPT])

    assert "expire" in backend.calls
    assert answers[0]["answer"] == "skip"


@pytest.mark.asyncio
async def test_a_question_hermes_settles_loses_its_card() -> None:
    backend = Backend(statuses=["pending"])

    persisted, answers = await _turn(backend, [PROMPT], settle_after=timedelta(seconds=90))

    assert answers == []
    assert "expire" in backend.calls
    assert [item["content"] for item in persisted] == ["fine"]


@pytest.mark.asyncio
async def test_a_long_wait_for_an_answer_keeps_the_runner() -> None:
    backend = Backend(statuses=["pending"])

    _, answers = await _turn(backend, [PROMPT], answer_after=timedelta(minutes=40))

    assert backend.touched >= 7
    assert answers[0]["answer"] == "Rome"
