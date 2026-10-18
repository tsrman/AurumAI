"""Pre-lifecycle workflow command sequence for real-history replay checks."""

from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel


class LegacyInput(BaseModel):
    document_id: str
    org_id: str
    knowledge_base_id: str


@workflow.defn(name="IndexDocumentWorkflow")
class LegacyWorkflow:
    # The pre-upgrade command sequence from c0d9070, including old failure args.
    @workflow.run
    async def run(self, input: LegacyInput) -> None:
        try:
            await workflow.execute_activity(
                "index_document",
                input,
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            await workflow.execute_activity(
                "mark_document_failed",
                args=[
                    input.document_id,
                    input.org_id,
                    input.knowledge_base_id,
                    str(exc.cause or exc)[:2000],
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
