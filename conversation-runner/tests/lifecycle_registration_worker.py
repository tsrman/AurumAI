"""Subprocess fixture: real runner registration activity against a private API."""

import asyncio
import os
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import httpx
    from src.config import settings
    from src.workflows.activities import register_conversation_execution
    from src.workflows.lifecycle import register_lifecycle_execution


@workflow.defn(name="EvaluationWorkflow")
class RegistrationSibling:
    @workflow.run
    async def run(self, data: dict) -> None:
        pass


@workflow.defn(name="ConversationWorkflow")
class RegistrationRoot:
    @workflow.run
    async def run(self, data: dict) -> None:
        if data.get("sibling"):
            await workflow.execute_child_workflow(
                RegistrationSibling.run,
                data,
                id=f"{data['conversation_id']}-eval-sibling",
            )
        await register_lifecycle_execution()
        await workflow.execute_activity(
            "registered_work", start_to_close_timeout=timedelta(seconds=5)
        )


@activity.defn
async def registered_work() -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{settings.llm_gateway_url}/test/work-started")
        response.raise_for_status()


async def main():
    client = await Client.connect(os.environ["REGISTRATION_TEMPORAL_HOST"])
    async with Worker(
        client,
        task_queue=f"session-{settings.conversation_id}",
        workflows=[RegistrationRoot, RegistrationSibling],
        activities=[register_conversation_execution, registered_work],
    ):
        print("READY", flush=True)
        # Parent closes only this worker after observing exact workflow closure.
        await asyncio.to_thread(input)


if __name__ == "__main__":
    asyncio.run(main())
