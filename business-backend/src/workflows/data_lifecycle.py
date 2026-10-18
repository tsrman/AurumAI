"""UUID-only maintenance orchestration, separate from identity and chat queues."""

from dataclasses import dataclass
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

DATA_LIFECYCLE_QUEUE = "platform-data-lifecycle"


@dataclass
class DataLifecycleInput:
    org_id: str
    conversation_id: str | None = None
    archive_id: str | None = None
    audit: bool = False


@workflow.defn
class DataLifecycleWorkflow:
    @workflow.run
    async def run(self, request: DataLifecycleInput) -> None:
        await workflow.execute_activity(
            "archive_data",
            request,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=5,
            ),
        )
