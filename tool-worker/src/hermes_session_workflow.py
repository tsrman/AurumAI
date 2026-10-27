"""The long-lived Hermes Agent session of one conversation (docs/HERMES_AGENT.md, phase 4).

ConversationWorkflow hands each Hermes turn over with signal-with-start (a
workflow cannot send another one an Update) and waits for the
`hermes_turn_result` signal back. Turns run one at a time: the sandbox is
prepared (reused, created, restored or resumed by skill-executor), the
session is bound to the turn and its execution, the run is followed to its
result, and the binding is removed again. Once no turn arrives for the idle
time, the sandbox is snapshotted and removed and the session closed, which
revokes its token; the next turn starts a new run of this workflow.

Every backend call uses a fresh runner token from the waiting conversation
runner, since one lives for thirty minutes and a turn for hours.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

HERMES_SESSION_WORKFLOW = "HermesSessionWorkflow"
TURN_SIGNAL = "hermes_turn"
CANCEL_SIGNAL = "hermes_cancel_turn"
# The user's answer to a question the running turn asked (see hermes_questions).
ANSWER_SIGNAL = "hermes_answer_question"
RESULT_SIGNAL = "hermes_turn_result"
# Continue-as-new after this many turns keeps the history bounded.
TURNS_PER_RUN = 50
# For the worker: heartbeats carry cancellation, so a cancelled turn stops its
# run within seconds rather than at 80% of the heartbeat timeout.
HEARTBEAT_THROTTLE = timedelta(seconds=5)
# Turns already answered, so a repeated hand-off is not run again.
_RECENT_TURNS = 100
# A runner that is gone never picks the task up; without a bound on the whole
# wait the session would hang and every later turn with it.
_TOKEN_WAIT = timedelta(minutes=2)
_RETRY = RetryPolicy(maximum_attempts=5, maximum_interval=timedelta(seconds=30))
_ONCE = RetryPolicy(maximum_attempts=1)
# Gone between preparing and running (retired by the reaper): prepared again once.
_REPLACED = frozenset({"hermes_sandbox_not_found", "hermes_sandbox_stopped"})


@dataclass
class HermesTurn:
    turn_id: str
    execution_id: str
    model: str
    text: str
    history: list[dict[str, Any]]
    # Where to answer and where the conversation's runner token comes from.
    parent_workflow_id: str
    parent_task_queue: str
    deadline_seconds: int = 7200


@dataclass
class HermesSessionInput:
    org_id: str
    conversation_id: str
    idle_seconds: int = 1800
    pending: list[HermesTurn] = field(default_factory=list)
    session_id: str | None = None
    parent_task_queue: str | None = None
    handled: list[str] = field(default_factory=list)


@workflow.defn(name=HERMES_SESSION_WORKFLOW)
class HermesSessionWorkflow:
    def __init__(self) -> None:
        self._pending: list[HermesTurn] = []
        self._cancelled: set[str] = set()
        self._running: str | None = None
        self._handled: list[str] = []
        self._answers: list[dict[str, Any]] = []

    @workflow.signal(name=TURN_SIGNAL)
    def turn(self, turn: HermesTurn) -> None:
        known = {queued.turn_id for queued in self._pending} | set(self._handled) | {self._running}
        if turn.turn_id not in known:
            self._pending.append(turn)

    @workflow.signal(name=CANCEL_SIGNAL)
    def cancel_turn(self, turn_id: str) -> None:
        self._cancelled.add(turn_id)

    @workflow.signal(name=ANSWER_SIGNAL)
    def answer_question(self, answer: dict[str, Any]) -> None:
        if answer.get("turn_id") == self._running and self._running is not None:
            self._answers.append(answer)

    @workflow.run
    async def run(self, start: HermesSessionInput) -> None:
        self._handled = list(start.handled)[-_RECENT_TURNS:]
        self._pending = [
            turn for turn in list(start.pending) + self._pending if turn.turn_id not in self._handled
        ]
        session_id, parent_queue = start.session_id, start.parent_task_queue
        handled = 0
        while True:
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending), timeout=timedelta(seconds=start.idle_seconds)
                )
            except asyncio.TimeoutError:
                await self._retire(session_id, parent_queue)
                if not self._pending:
                    return
                session_id = None
            turn = self._pending.pop(0)
            parent_queue = turn.parent_task_queue
            if turn.turn_id in self._cancelled:
                result: dict[str, Any] = {"status": "cancelled"}
            else:
                self._running = turn.turn_id
                session_id, result = await self._run_turn(start, turn, session_id)
                self._running = None
            self._cancelled.discard(turn.turn_id)
            self._handled = (self._handled + [turn.turn_id])[-_RECENT_TURNS:]
            try:
                await workflow.get_external_workflow_handle(turn.parent_workflow_id).signal(
                    RESULT_SIGNAL, {"turn_id": turn.turn_id, **result}
                )
            except Exception:
                # The conversation workflow is gone (reset or terminated);
                # the session still serves the turns that follow.
                workflow.logger.warning("Could not answer the conversation workflow")
            handled += 1
            if handled >= TURNS_PER_RUN and not self._pending:
                workflow.continue_as_new(
                    HermesSessionInput(
                        org_id=start.org_id,
                        conversation_id=start.conversation_id,
                        idle_seconds=start.idle_seconds,
                        session_id=session_id,
                        parent_task_queue=parent_queue,
                        handled=self._handled,
                    )
                )

    async def _token(self, task_queue: str) -> str:
        token = await workflow.execute_activity(
            "get_session_token",
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=15),
            schedule_to_close_timeout=_TOKEN_WAIT,
            retry_policy=_RETRY,
            result_type=str,
        )
        if not token:
            raise ApplicationError("hermes_runner_token_missing", non_retryable=True)
        return str(token)

    async def _prepare(self, start: HermesSessionInput, turn: HermesTurn) -> str:
        session_id: str = await workflow.execute_activity(
            "hermes_prepare_sandbox",
            args=(
                await self._token(turn.parent_task_queue),
                start.org_id,
                start.conversation_id,
                turn.model,
                turn.turn_id,
            ),
            start_to_close_timeout=timedelta(minutes=25),
            retry_policy=_RETRY,
            result_type=str,
        )
        await workflow.execute_activity(
            "hermes_bind_turn",
            args=(await self._token(turn.parent_task_queue), session_id, turn.turn_id, turn.execution_id),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_RETRY,
        )
        return session_id

    async def _run_turn(
        self, start: HermesSessionInput, turn: HermesTurn, session_id: str | None
    ) -> tuple[str | None, dict[str, Any]]:
        try:
            session_id = await self._prepare(start, turn)
            for attempt in range(2):
                try:
                    return session_id, await self._follow(start, turn, session_id)
                except ActivityError as exc:
                    if attempt == 0 and _code(exc) in _REPLACED:
                        session_id = await self._prepare(start, turn)
                        continue
                    raise
            raise ApplicationError("hermes_interrupted")
        except ActivityError as exc:
            if turn.turn_id in self._cancelled:
                return session_id, {"status": "cancelled"}
            return session_id, {"status": "failed", "error": _code(exc) or "hermes_interrupted"}
        except ApplicationError as exc:
            return session_id, {"status": "failed", "error": exc.type or str(exc)}
        finally:
            if session_id is not None:
                try:
                    await workflow.execute_activity(
                        "hermes_unbind_turn",
                        args=(await self._token(turn.parent_task_queue), session_id),
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_RETRY,
                    )
                except ActivityError:
                    # The binding ends with the turn anyway: every check needs
                    # the bound turn to be active.
                    workflow.logger.warning("Could not unbind the Hermes session")

    async def _follow(self, start: HermesSessionInput, turn: HermesTurn, session_id: str) -> dict[str, Any]:
        run = workflow.start_activity(
            "hermes_run_turn",
            args=(
                session_id,
                turn.turn_id,
                start.conversation_id,
                turn.text,
                turn.history,
                start.org_id,
                turn.parent_workflow_id,
            ),
            # The deadline covers every attempt, not each one.
            schedule_to_close_timeout=timedelta(seconds=turn.deadline_seconds),
            heartbeat_timeout=timedelta(seconds=60),
            # A lost worker's retry attaches to the same run by its key.
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=list(_REPLACED)),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            result_type=dict,
        )
        while True:
            woken = asyncio.ensure_future(
                workflow.wait_condition(lambda: turn.turn_id in self._cancelled or bool(self._answers))
            )
            done, _ = await workflow.wait([run, woken], return_when=asyncio.FIRST_COMPLETED)
            if run in done:
                woken.cancel()
                self._answers.clear()
                return dict(await run)
            if turn.turn_id in self._cancelled:
                run.cancel()
                try:
                    await run
                except (ActivityError, asyncio.CancelledError):
                    pass
                return {"status": "cancelled"}
            await self._answer(session_id, self._answers.pop(0))

    async def _answer(self, session_id: str, answer: dict[str, Any]) -> None:
        try:
            await workflow.execute_activity(
                "hermes_answer_question",
                args=(
                    session_id,
                    str(answer.get("run_id")),
                    str(answer.get("prompt_id")),
                    answer.get("execution_generation"),
                    str(answer.get("answer")),
                ),
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=RetryPolicy(maximum_attempts=3, maximum_interval=timedelta(seconds=10)),
                result_type=str,
            )
        except ActivityError:
            # Hermes's own timeout ends a question nobody answers; the turn goes on.
            workflow.logger.warning("Could not answer a Hermes question")

    async def _retire(self, session_id: str | None, parent_queue: str | None) -> None:
        if session_id is None:
            return
        try:
            await workflow.execute_activity(
                "hermes_retire_sandbox",
                args=(session_id,),
                start_to_close_timeout=timedelta(minutes=25),
                retry_policy=_RETRY,
            )
        except ActivityError:
            # skill-executor's reaper retires it at its idle lifetime instead.
            workflow.logger.warning("Could not retire the Hermes sandbox")
        if parent_queue is None:
            return
        try:
            # The runner may have gone idle as well; then the session expires.
            token = await workflow.execute_activity(
                "get_session_token",
                task_queue=parent_queue,
                schedule_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ONCE,
                result_type=str,
            )
            if token:
                await workflow.execute_activity(
                    "hermes_close_session",
                    args=(token, session_id),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_RETRY,
                )
        except ActivityError:
            workflow.logger.warning("Could not close the idle Hermes session")


def _code(exc: ActivityError) -> str | None:
    cause = exc.cause
    return cause.type if isinstance(cause, ApplicationError) else None
