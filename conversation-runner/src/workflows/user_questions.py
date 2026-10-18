"""Durable Temporal wait state for platform-owned user questions."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_RECONCILE_INTERVAL = timedelta(seconds=30)
_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass
class QuestionWaitState:
    _versions: dict[str, int] = field(default_factory=dict)

    def notify(self, question_id: str, version: int) -> None:
        self._versions[question_id] = max(version, self._versions.get(question_id, 0))

    def version(self, question_id: str) -> int:
        return self._versions.get(question_id, 0)


def question_deadline(question: dict[str, Any]) -> datetime:
    raw = question.get("expiresAt")
    if not isinstance(raw, str):
        raise ApplicationError(
            "workflow_unavailable", type="workflow_unavailable", non_retryable=True
        )
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApplicationError(
            "workflow_unavailable", type="workflow_unavailable", non_retryable=True
        ) from exc


async def _question_activity(name: str, question_id: str) -> dict[str, Any]:
    result = await workflow.execute_activity(
        name,
        args=(question_id,),
        start_to_close_timeout=_ACTIVITY_TIMEOUT,
        retry_policy=_RETRY_POLICY,
    )
    if not isinstance(result, dict) or result.get("questionId", question_id) != question_id:
        raise ApplicationError(
            "workflow_unavailable", type="workflow_unavailable", non_retryable=True
        )
    return result


def _terminal_error(status: object) -> ApplicationError | None:
    if status == "expired":
        return ApplicationError(
            "user_input_timeout", type="user_input_timeout", non_retryable=True
        )
    if status == "cancelled":
        return ApplicationError(
            "user_input_cancelled", type="user_input_cancelled", non_retryable=True
        )
    if status != "pending":
        return ApplicationError(
            "workflow_unavailable", type="workflow_unavailable", non_retryable=True
        )
    return None


def application_error_type(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ApplicationError):
            return current.type
        current = current.__cause__
    return None


async def wait_for_answer(
    state: QuestionWaitState,
    question_id: str,
    deadline: datetime,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Reconcile durable state around signals/timers; never hold a long activity."""
    is_cancelled = cancelled or (lambda: False)
    while True:
        if is_cancelled():
            raise ApplicationError(
                "user_input_cancelled", type="user_input_cancelled", non_retryable=True
            )
        question = await _question_activity("read_user_question", question_id)
        if is_cancelled():
            raise ApplicationError(
                "user_input_cancelled", type="user_input_cancelled", non_retryable=True
            )
        status = question.get("status")
        if status == "answered":
            return await _question_activity("consume_user_question", question_id)
        terminal_error = _terminal_error(status)
        if terminal_error is not None:
            raise terminal_error

        remaining = deadline - workflow.now()
        if remaining <= timedelta(0):
            settled = await _question_activity("expire_user_question", question_id)
            if settled.get("status") == "answered":
                continue
            terminal_error = _terminal_error(settled.get("status"))
            if terminal_error is not None:
                raise terminal_error
            raise ApplicationError(
                "workflow_unavailable", type="workflow_unavailable", non_retryable=True
            )

        observed = state.version(question_id)
        try:
            await workflow.wait_condition(
                lambda: state.version(question_id) > observed or is_cancelled(),
                timeout=min(_RECONCILE_INTERVAL, remaining),
            )
        except asyncio.TimeoutError:
            # A bounded DB reconciliation follows every timer. The timeout
            # itself never overwrites a concurrently accepted answer.
            pass
