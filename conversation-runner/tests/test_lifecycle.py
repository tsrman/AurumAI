"""A failed registration barrier must prevent all execution work."""

from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows.lifecycle import register_lifecycle_execution


@workflow.defn
class LifecycleBarrierWorkflow:
    @workflow.run
    async def run(self) -> None:
        await register_lifecycle_execution()
        await workflow.execute_activity("new_work", start_to_close_timeout=timedelta(seconds=5))


@pytest.mark.asyncio
async def test_registration_failure_prevents_new_work():
    work = []

    @activity.defn(name="register_conversation_execution")
    async def registration(workflow_id: str, run_id: str) -> None:
        raise ApplicationError("registration unavailable", non_retryable=True)

    @activity.defn(name="new_work")
    async def new_work() -> None:
        work.append("started")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="registration-test",
            workflows=[LifecycleBarrierWorkflow],
            activities=[registration, new_work],
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    LifecycleBarrierWorkflow.run,
                    id="registration-barrier",
                    task_queue="registration-test",
                )
    assert work == []
