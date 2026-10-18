from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel


class IndexDocumentInput(BaseModel):
    document_id: str
    org_id: str
    knowledge_base_id: str
    generation: int = 1
    operation_id: str | None = None


@workflow.defn(name="IndexDocumentWorkflow")
class IndexDocumentWorkflow:
    @workflow.run
    async def run(self, input: IndexDocumentInput) -> None:
        # A queued pre-upgrade input has no durable operation even on its first
        # (non-replay) task. Keep its ordinary fenced indexing path and the
        # migration's incomplete archive provenance; never invent a receipt.
        if workflow.patched("rag-lifecycle-v1") and input.operation_id is not None:
            await workflow.execute_activity(
                "register_index_execution",
                input,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        try:
            await workflow.execute_activity(
                "index_document",
                input,
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            await workflow.execute_activity(
                "mark_document_failed",
                args=[
                    input.document_id,
                    input.org_id,
                    input.knowledge_base_id,
                    "indexing_failed",
                    input.generation,
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
