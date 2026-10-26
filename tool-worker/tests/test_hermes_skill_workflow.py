import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.hermes_skill_workflow import HERMES_SKILL_WORKFLOW, HermesSkillCall, HermesSkillCallWorkflow

QUEUE = "tools-test"


async def _run(fake: Any, call: dict[str, Any], workflow_id: str | None = None) -> Any:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[HermesSkillCallWorkflow], activities=[fake]):
            # The backend starts it by name with a plain dict, as here.
            return await env.client.execute_workflow(
                HERMES_SKILL_WORKFLOW, call, id=workflow_id or str(uuid.uuid4()), task_queue=QUEUE
            )


CALL = {"skill_id": "s-1", "conversation_id": "c-1", "session_token": "t", "arguments": {"q": 1}}


@pytest.mark.asyncio
async def test_runs_execute_skill_with_the_call_and_keeps_only_the_output() -> None:
    seen: list[tuple[Any, ...]] = []

    @activity.defn(name="execute_skill")
    async def fake(skill_id: str, args: dict[str, Any], conversation_id: str, session_token: str) -> dict[str, Any]:
        seen.append((skill_id, args, conversation_id, session_token))
        return {"skill_id": skill_id, "output": {"answer": 42}}

    result = await _run(fake, CALL)

    assert seen == [("s-1", {"q": 1}, "c-1", "t")]
    assert result == {"output": {"answer": 42}}


@pytest.mark.asyncio
async def test_a_failed_skill_completes_with_a_bare_error_code() -> None:
    attempts: list[int] = []

    @activity.defn(name="execute_skill")
    async def fake(*_args: Any) -> dict[str, Any]:
        attempts.append(1)
        raise ApplicationError("https://secret.example/token=abc failed", non_retryable=True)

    result = await _run(fake, CALL)

    assert result == {"error": "skill_failed"}
    assert attempts == [1]


@pytest.mark.asyncio
async def test_transient_failures_are_retried_a_bounded_number_of_times() -> None:
    attempts: list[int] = []

    @activity.defn(name="execute_skill")
    async def fake(*_args: Any) -> dict[str, Any]:
        attempts.append(1)
        raise RuntimeError("upstream down")

    result = await _run(fake, CALL)

    assert result == {"error": "skill_failed"}
    assert len(attempts) == 3


def test_the_call_defaults_to_no_arguments() -> None:
    assert HermesSkillCall(skill_id="s", conversation_id="c", session_token="t").arguments == {}
