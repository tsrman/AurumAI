"""The archive purge's part in Hermes Agent: a conversation's sandboxes and
workspace snapshots go (docs/HERMES_AGENT.md, phase 4c-ii).

business-backend's archive purge starts it after the archive copies are
verified and before the conversation's rows go, and waits for it; only
tool-worker reaches skill-executor, whose credentials own the snapshots.
Repeating it is harmless, so a resumed purge simply runs it again.
"""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

HERMES_PURGE_WORKFLOW = "HermesConversationPurgeWorkflow"


@dataclass(frozen=True)
class HermesPurge:
    org_id: str
    conversation_id: str


@workflow.defn(name=HERMES_PURGE_WORKFLOW)
class HermesConversationPurgeWorkflow:
    @workflow.run
    async def run(self, purge: HermesPurge) -> dict[str, int]:
        result: dict[str, int] = await workflow.execute_activity(
            "hermes_purge_conversation",
            args=(purge.org_id, purge.conversation_id),
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=5, maximum_interval=timedelta(minutes=1)),
            result_type=dict,
        )
        return result
