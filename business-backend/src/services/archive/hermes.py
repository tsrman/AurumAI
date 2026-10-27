"""The archive purge's step for Hermes Agent: tool-worker's
HermesConversationPurgeWorkflow removes the conversation's sandboxes and
workspace snapshots, which only skill-executor's credentials reach."""

from datetime import timedelta
from uuid import UUID

from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from src.core.config import settings

HERMES_PURGE_WORKFLOW = "HermesConversationPurgeWorkflow"
_TIMEOUT = timedelta(minutes=30)


class HermesSnapshotPurge:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    async def __call__(self, org_id: UUID, conversation_id: UUID) -> None:
        if self._client is None:
            self._client = await Client.connect(settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE)
        # One id per conversation: a resumed purge attaches to a run still
        # going, or starts the harmless repeat.
        handle = await self._client.start_workflow(
            HERMES_PURGE_WORKFLOW,
            {"org_id": str(org_id), "conversation_id": str(conversation_id)},
            id=f"hermes-purge-{org_id}-{conversation_id}",
            task_queue=settings.HERMES_SKILL_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            execution_timeout=_TIMEOUT,
        )
        try:
            await handle.result()
        except WorkflowFailureError as exc:
            raise RuntimeError("hermes_purge_failed") from exc
