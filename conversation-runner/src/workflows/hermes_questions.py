"""Hermes Agent's questions to the user, asked through the platform's question
cards (docs/HERMES_AGENT.md, phase 4c).

tool-worker forwards each question Hermes's clarify tool asks; the
conversation workflow asks it as an ordinary question of the turn, one at a
time, and sends the answer to the session workflow, which hands it to the
run. A question the user cannot be asked (questions off, the turn's limit
reached) or does not answer in time is answered "skip", so Hermes goes on
instead of waiting out its own timeout. Each step is a short activity or a
signal: the conversation workflow keeps its five-minute touches meanwhile.
"""

import uuid
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from src.workflows.activities import hermes_session_id
    from src.workflows.builtin_tools import ExecutionContext
    from src.workflows.user_questions import QuestionWaitState, application_error_type, question_deadline

ANSWER_SIGNAL = "hermes_answer_question"
SKIP = "skip"
# How often a pending question is read back, besides its own signal.
RECONCILE = timedelta(seconds=30)
_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_RETRY = RetryPolicy(maximum_attempts=3)
_NAMESPACE = uuid.UUID("5d0e8f3a-7c2b-4e61-9a8d-3f1b2c4d5e6f")


class HermesQuestions:
    """The questions of the conversation's active Hermes turn."""

    def __init__(self, waits: QuestionWaitState) -> None:
        self._waits = waits
        self._turn_id: str | None = None
        self._queued: list[dict[str, Any]] = []
        self._seen: set[str] = set()
        self._settled: set[str] = set()
        self._asking: dict[str, Any] | None = None

    def start(self, turn_id: str) -> None:
        self._turn_id, self._queued, self._seen, self._settled, self._asking = turn_id, [], set(), set(), None

    def asked(self, question: dict[str, Any]) -> None:
        """A question Hermes asks; a replay after a reattach repeats it."""
        prompt_id = question.get("prompt_id")
        if question.get("turn_id") != self._turn_id or not isinstance(prompt_id, str) or prompt_id in self._seen:
            return
        self._seen.add(prompt_id)
        self._queued.append(question)

    def settled(self, settled: dict[str, Any]) -> None:
        if settled.get("turn_id") == self._turn_id and isinstance(settled.get("prompt_id"), str):
            self._settled.add(settled["prompt_id"])

    @property
    def waiting(self) -> bool:
        return self._asking is not None

    def changed(self) -> bool:
        """Something to act on: a new question, or news about the one asked."""
        if self._asking is None:
            return bool(self._queued)
        return (
            self._asking["prompt"]["prompt_id"] in self._settled
            or self._waits.version(self._asking["question_id"]) > self._asking["version"]
        )

    async def step(self, conversation_id: str, execution: ExecutionContext, enabled: bool) -> None:
        if self._asking is None and self._queued:
            await self._ask(conversation_id, execution, self._queued.pop(0), enabled)
        if self._asking is not None:
            try:
                await self._check(conversation_id)
            except ActivityError:
                # Read again on the next slice.
                workflow.logger.warning("Could not read a Hermes question")

    async def close(self) -> None:
        """The turn ended: the card of an unanswered question goes too."""
        if self._asking is not None:
            await self._expire(self._asking["question_id"])
            self._asking = None

    async def _ask(
        self, conversation_id: str, execution: ExecutionContext, prompt: dict[str, Any], enabled: bool
    ) -> None:
        if prompt["prompt_id"] in self._settled:
            return
        if not enabled:
            await self._answer(conversation_id, prompt, SKIP)
            return
        choices = [str(choice) for choice in prompt.get("choices") or []]
        request = {
            "text": str(prompt.get("text") or ""),
            "options": [{"id": f"c{index}", "label": choice} for index, choice in enumerate(choices)],
        }
        invocation = str(uuid.uuid5(_NAMESPACE, f"{self._turn_id}:{prompt['prompt_id']}"))
        try:
            question = await workflow.execute_activity(
                "create_user_question",
                args=(execution, invocation, request),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY,
                result_type=dict,
            )
            question_id = str(question["questionId"])
            deadline = question_deadline(question)
        except (ActivityError, ApplicationError, KeyError) as exc:
            workflow.logger.warning(
                f"Could not ask Hermes's question: {application_error_type(exc) or type(exc).__name__}"
            )
            await self._answer(conversation_id, prompt, SKIP)
            return
        self._asking = {
            "prompt": prompt,
            "question_id": question_id,
            "deadline": deadline,
            "version": self._waits.version(question_id),
        }

    async def _check(self, conversation_id: str) -> None:
        asking = self._asking
        assert asking is not None
        prompt, question_id = asking["prompt"], asking["question_id"]
        if prompt["prompt_id"] in self._settled:
            # Hermes stopped waiting (its timeout, or the run ended).
            await self._expire(question_id)
            self._asking = None
            return
        asking["version"] = self._waits.version(question_id)
        status = await self._read(question_id)
        if status == "pending" and workflow.now() >= asking["deadline"]:
            status = (await self._activity("expire_user_question", question_id)).get("status")
        if status == "pending":
            return
        answer = SKIP
        if status == "answered":
            consumed = await self._activity("consume_user_question", question_id)
            answer = _answer_text(consumed, prompt) or SKIP
        self._asking = None
        await self._answer(conversation_id, prompt, answer)

    async def _read(self, question_id: str) -> object:
        return (await self._activity("read_user_question", question_id)).get("status")

    async def _expire(self, question_id: str) -> None:
        try:
            await self._activity("expire_user_question", question_id)
        except ActivityError:
            workflow.logger.warning("Could not expire a Hermes question")

    @staticmethod
    async def _activity(name: str, question_id: str) -> dict[str, Any]:
        result = await workflow.execute_activity(
            name,
            args=(question_id,),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
            result_type=dict,
        )
        return result if isinstance(result, dict) else {}

    async def _answer(self, conversation_id: str, prompt: dict[str, Any], answer: str) -> None:
        payload = {
            "turn_id": self._turn_id,
            "run_id": prompt.get("run_id"),
            "prompt_id": prompt.get("prompt_id"),
            "execution_generation": prompt.get("execution_generation"),
            "answer": answer,
        }
        try:
            await workflow.get_external_workflow_handle(hermes_session_id(conversation_id)).signal(
                ANSWER_SIGNAL, payload
            )
        except Exception:
            workflow.logger.warning("Could not send an answer to the Hermes session")


def _answer_text(answer: dict[str, Any], prompt: dict[str, Any]) -> str | None:
    """The chosen choice as Hermes offered it, or the user's own text."""
    option = answer.get("optionId")
    choices = [str(choice) for choice in prompt.get("choices") or []]
    if isinstance(option, str) and option.startswith("c") and option[1:].isdigit():
        index = int(option[1:])
        return choices[index] if index < len(choices) else None
    text = answer.get("text")
    return text if isinstance(text, str) and text.strip() else None
