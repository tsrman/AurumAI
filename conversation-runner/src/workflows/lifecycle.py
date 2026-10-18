"""Replay-patched registration barrier before any new execution performs work."""

from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy


async def register_lifecycle_execution() -> None:
    if workflow.patched("conversation-lifecycle-v1"):
        info = workflow.info()
        await workflow.execute_activity(
            "register_conversation_execution",
            args=(info.workflow_id, info.run_id),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=15)
            ),
        )
