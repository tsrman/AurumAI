"""Deterministic identity workflow. Its history contains only identity coordinates."""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

IDENTITY_SYNC_QUEUE = "platform-identity-sync"


@dataclass(frozen=True)
class IdentitySyncInput:
    org_id: str
    user_id: str
    generation: int


@workflow.defn
class IdentitySyncWorkflow:
    @workflow.run
    async def run(self, request: IdentitySyncInput) -> None:
        await workflow.execute_activity(
            "apply_identity_snapshot",
            request,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=60),
                maximum_attempts=5,
            ),
        )
