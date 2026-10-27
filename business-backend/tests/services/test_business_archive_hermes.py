"""The archive purge removes a conversation's Hermes snapshots through tool-worker."""

import uuid
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from src.services.archive.hermes import HERMES_PURGE_WORKFLOW, HermesSnapshotPurge

purged: list[tuple[str, str]] = []


@workflow.defn(name=HERMES_PURGE_WORKFLOW)
class FakePurge:
    @workflow.run
    async def run(self, purge: dict[str, Any]) -> dict[str, int]:
        from datetime import timedelta

        return await workflow.execute_activity(
            "hermes_purge_conversation",
            args=(purge["org_id"], purge["conversation_id"]),
            start_to_close_timeout=timedelta(seconds=10),
        )


@activity.defn(name="hermes_purge_conversation")
async def purge_conversation(org_id: str, conversation_id: str) -> dict[str, int]:
    purged.append((org_id, conversation_id))
    return {"sandboxes": 0, "snapshots": 2}


@pytest.mark.asyncio
async def test_the_purge_runs_tool_workers_workflow_and_can_repeat(monkeypatch) -> None:
    from src.services.archive import hermes

    monkeypatch.setattr(hermes.settings, "HERMES_SKILL_TASK_QUEUE", "tools-test")
    org, conversation = uuid.uuid4(), uuid.uuid4()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tools-test",
            workflows=[FakePurge],
            activities=[purge_conversation],
            # The test module imports the backend.
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            purge = HermesSnapshotPurge(env.client)
            await purge(org, conversation)
            await purge(org, conversation)

    assert purged == [(str(org), str(conversation))] * 2
