# ruff: noqa: F811
"""Real Temporal executes the production UUID-only data activity and durable resume."""

import os
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4
import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker
from src.core.config import settings
from src.maintenance_worker import archive_data
from src.workflows.data_lifecycle import DataLifecycleWorkflow, DataLifecycleInput
from src.services.archive.service import ArchiveService
from src.services.archive import runtime
from src.services.conversation_lifecycle import ArchiveBlocked
from tests.services.test_business_archive_service import archive_fixture, LocalArchiveStorage  # noqa:F401


@pytest.mark.skipif(
    not os.getenv("ARCHIVE_TEST_TEMPORAL_HOST"), reason="isolated real Temporal required"
)
@pytest.mark.parametrize("new_claims", [True, False])
async def test_real_data_workflow_and_flag_off_resume(
    archive_fixture, tmp_path, monkeypatch, new_claims
):
    sessions, org, conversation = archive_fixture
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    archive_id = str(uuid4())
    if not new_claims:
        original = service._seal

        async def pause(*args):
            raise ArchiveBlocked("test_stopped_after_claim")

        monkeypatch.setattr(service, "_seal", pause)
        with pytest.raises(ArchiveBlocked, match="test_stopped_after_claim"):
            await service.run(org, conversation, apply=True)
        archive_id = (await service.list(org))[0]["archive_id"]
        monkeypatch.setattr(service, "_seal", original)

    @asynccontextmanager
    async def context():
        yield service

    monkeypatch.setattr(runtime, "archive_service", context)
    monkeypatch.setattr(settings, "ARCHIVE_ENABLED", new_claims)
    host = os.environ["ARCHIVE_TEST_TEMPORAL_HOST"]
    assert host == "127.0.0.1:57233"
    client = await Client.connect(host, namespace="default")
    workflow_id = f"archive-data-test-{uuid4()}"
    queue = f"archive-data-test-{uuid4()}"
    request = DataLifecycleInput(
        str(org), conversation_id=str(conversation) if new_claims else None, archive_id=archive_id
    )
    journal = Path("/tmp/business-archive-temporal-journal.jsonl")
    async with Worker(
        client, task_queue=queue, workflows=[DataLifecycleWorkflow], activities=[archive_data]
    ):
        handle = await client.start_workflow(
            DataLifecycleWorkflow.run, request, id=workflow_id, task_queue=queue
        )
        with journal.open("a") as target:
            journal.chmod(0o600)
            target.write(
                json.dumps(
                    {
                        "namespace": "default",
                        "workflow_id": workflow_id,
                        "run_id": handle.first_execution_run_id,
                        "queue": queue,
                    }
                )
                + "\n"
            )
        await handle.result()
        assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    job = (await service.list(org))[0]
    assert job["state"] == "purged"
    await service.restore(org, UUID(job["archive_id"]), apply=True)
