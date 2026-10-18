from dataclasses import asdict
from uuid import uuid4

import pytest


def test_workflow_input_has_identifiers_only():
    from src.workflows.identity_sync import IdentitySyncInput

    request = IdentitySyncInput(str(uuid4()), str(uuid4()), 1)
    assert set(asdict(request)) == {"org_id", "user_id", "generation"}


@pytest.mark.asyncio
async def test_dispatcher_recovers_enqueue_without_api_temporal(db, test_org):
    from src.services.identity_sync import submit_identity
    from src.maintenance_worker import dispatch_identity_batch
    from tests.services.test_identity_sync import claims
    from unittest.mock import AsyncMock

    user_id = uuid4()
    await submit_identity(db, claims(test_org.id, user_id, []))
    client = AsyncMock()
    # Existing transaction fixture is visible only on this connection.
    count = await dispatch_identity_batch(client, sessions=lambda: db)
    assert count >= 1
    request = client.start_workflow.call_args.args[1]
    assert request.org_id == str(test_org.id)
    assert request.user_id == str(user_id)


def test_health_requires_fresh_atomic_heartbeat(tmp_path, monkeypatch):
    import time
    from src import maintenance_worker

    path = tmp_path / "identity-heartbeat"
    monkeypatch.setattr(maintenance_worker, "HEARTBEAT_PATH", path)
    assert not maintenance_worker.identity_healthy()
    maintenance_worker.write_identity_heartbeat()
    assert maintenance_worker.identity_healthy()
    path.write_text(str(time.monotonic() - 120))
    assert not maintenance_worker.identity_healthy()
    path.write_text("invalid")
    assert not maintenance_worker.identity_healthy()


@pytest.mark.asyncio
async def test_real_temporal_worker_retries_after_commit_and_recovers_inbox(
    identity_database, monkeypatch
):
    from src import maintenance_worker
    from src.core.config import settings
    from src.services.identity_sync import submit_identity, require_applied_identity
    from src.workflows.identity_sync import IDENTITY_SYNC_QUEUE, IdentitySyncWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from tests.services.test_identity_sync import claims

    sessions, org_id, user_id = identity_database
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(maintenance_worker, "async_session", sessions)
    original_apply = maintenance_worker.apply_identity
    calls = 0

    async def apply_then_fail_once(*args):
        nonlocal calls
        calls += 1
        await original_apply(*args)
        if calls == 1:
            raise RuntimeError("private snapshot must not leak")

    monkeypatch.setattr(maintenance_worker, "apply_identity", apply_then_fail_once)
    user = claims(org_id, user_id, ["finance"])
    user.email = f"{user_id}@example.com"
    # API process has committed and exited before any worker or Temporal start.
    async with sessions() as api:
        await submit_identity(api, user)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=IDENTITY_SYNC_QUEUE,
            workflows=[IdentitySyncWorkflow],
            activities=[maintenance_worker.apply_identity_snapshot],
        ):
            await maintenance_worker.dispatch_identity_batch(env.client, sessions=sessions)
            handle = env.client.get_workflow_handle(f"identity-sync:{org_id}:{user_id}:1")
            await handle.result()
            assert calls == 2
            history = await handle.fetch_history()
            assert "private snapshot" not in history.to_json()
            assert user.email not in history.to_json()
    async with sessions() as session:
        session.info["org_id"] = str(org_id)
        await require_applied_identity(session, org_id, user_id)


@pytest.mark.asyncio
async def test_application_failure_records_only_safe_category(db, test_org, monkeypatch):
    from src import maintenance_worker
    from src.models.identity_sync import IdentitySyncInbox
    from src.services.identity_sync import submit_identity
    from src.workflows.identity_sync import IdentitySyncInput
    from temporalio.exceptions import ApplicationError
    from tests.services.test_identity_sync import claims
    from unittest.mock import AsyncMock

    org_id, user_id = test_org.id, uuid4()
    await submit_identity(db, claims(org_id, user_id, []))
    monkeypatch.setattr(maintenance_worker, "async_session", lambda: db)
    monkeypatch.setattr(
        maintenance_worker, "apply_identity", AsyncMock(side_effect=RuntimeError("private"))
    )
    with pytest.raises(ApplicationError, match="identity_application_failed"):
        await maintenance_worker.apply_identity_snapshot(
            IdentitySyncInput(str(org_id), str(user_id), 1)
        )
    inbox = await db.get(IdentitySyncInbox, (org_id, user_id, 1))
    assert inbox.error_category == "application_failed"
