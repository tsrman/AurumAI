"""Independent maintenance process: no API lifespan or conversation runtime imports."""

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from src.core.config import settings
from src.core.database import async_session, engine
from src.models.identity_sync import IdentitySyncInbox
from src.workflows.data_lifecycle import DataLifecycleInput
from src.services.identity_sync import apply_identity
from src.workflows.identity_sync import IDENTITY_SYNC_QUEUE, IdentitySyncInput, IdentitySyncWorkflow

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], AsyncSession]
HEARTBEAT_PATH = Path("/tmp/platform-identity-heartbeat")


def write_identity_heartbeat() -> None:
    temporary = HEARTBEAT_PATH.with_suffix(f".{os.getpid()}.tmp")
    try:
        with temporary.open("w") as output:
            os.chmod(temporary, 0o600)
            output.write(str(time.monotonic()))
        temporary.replace(HEARTBEAT_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def identity_healthy() -> bool:
    try:
        age = time.monotonic() - float(HEARTBEAT_PATH.read_text())
        return 0 <= age <= 30
    except (OSError, ValueError):
        return False


@activity.defn(name="apply_identity_snapshot")
async def apply_identity_snapshot(request: IdentitySyncInput) -> None:
    # Do not let SQL/HTTP exception text copy private profile data into history.
    try:
        async with async_session() as session:
            session.info["org_id"] = str(UUID(request.org_id))
            await apply_identity(
                session, UUID(request.org_id), UUID(request.user_id), request.generation
            )
    except Exception:
        try:
            async with asyncio.timeout(5):
                async with async_session() as failure_session:
                    failure_session.info["org_id"] = str(UUID(request.org_id))
                    inbox = await failure_session.get(
                        IdentitySyncInbox,
                        (UUID(request.org_id), UUID(request.user_id), request.generation),
                    )
                    if inbox is not None:
                        inbox.error_category = "application_failed"
                        await failure_session.commit()
        except Exception:
            logger.warning("identity_failure_record_unavailable")
        raise ApplicationError("identity_application_failed", type="IdentityApplyError") from None


async def dispatch_identity_batch(
    client: Client,
    *,
    sessions: SessionFactory = async_session,
) -> int:
    async with sessions() as discovery:
        rows = (
            await discovery.execute(text("SELECT * FROM public.identity_sync_dispatch_batch(100)"))
        ).all()
    dispatched = 0
    for org_id, user_id, generation in rows:
        async with sessions() as session:
            session.info["org_id"] = str(org_id)
            inbox = await session.get(IdentitySyncInbox, (org_id, user_id, generation))
            if inbox is None:
                continue
            now = datetime.now(timezone.utc)
            # Multiple dispatchers may see one row; stable Temporal ID fences starts.
            if inbox.next_dispatch_at > now:
                continue
            inbox.attempts = min(inbox.attempts + 1, 31)
            inbox.next_dispatch_at = now + timedelta(seconds=min(2**inbox.attempts, 60))
            await session.commit()
        category: str | None = None
        try:
            await client.start_workflow(
                IdentitySyncWorkflow.run,
                IdentitySyncInput(str(org_id), str(user_id), generation),
                id=f"identity-sync:{org_id}:{user_id}:{generation}",
                task_queue=IDENTITY_SYNC_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                execution_timeout=timedelta(minutes=10),
            )
            dispatched += 1
        except WorkflowAlreadyStartedError:
            # Running or completed application; activity commit removes the inbox.
            dispatched += 1
        except Exception:
            category = "temporal_unavailable"
        if category is not None:
            async with sessions() as session:
                session.info["org_id"] = str(org_id)
                inbox = await session.get(IdentitySyncInbox, (org_id, user_id, generation))
                if inbox is not None:
                    inbox.error_category = category
                    await session.commit()
    return dispatched


async def _dispatch_loop(client: Client) -> None:
    while True:
        try:
            async with asyncio.timeout(10):
                await dispatch_identity_batch(client)
                if not await client.service_client.check_health():
                    raise RuntimeError("temporal_unhealthy")
            write_identity_heartbeat()
        except Exception:
            # No raw exception payload: database errors can contain bound snapshots.
            logger.warning("identity_dispatch_unavailable")
        await asyncio.sleep(1)


async def run_identity_worker() -> None:
    HEARTBEAT_PATH.unlink(missing_ok=True)
    engine.echo = False
    engine.sync_engine.hide_parameters = True
    if not settings.IDENTITY_SYNC_ENABLED:
        logger.info("identity_sync_disabled")
        return
    client = await Client.connect(settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE)
    worker = Worker(
        client,
        task_queue=IDENTITY_SYNC_QUEUE,
        workflows=[IdentitySyncWorkflow],
        activities=[apply_identity_snapshot],
        max_concurrent_activities=8,
        max_concurrent_workflow_tasks=8,
    )
    try:
        async with worker:
            await _dispatch_loop(client)
    finally:
        HEARTBEAT_PATH.unlink(missing_ok=True)
        await engine.dispose()


DATA_HEARTBEAT_PATH = Path("/tmp/platform-data-heartbeat")


def data_healthy() -> bool:
    try:
        age = time.monotonic() - float(DATA_HEARTBEAT_PATH.read_text())
        return 0 <= age <= 90
    except (OSError, ValueError):
        return False


@activity.defn(name="archive_data")
async def archive_data(request: "DataLifecycleInput") -> None:
    from src.services.archive.runtime import archive_service
    from src.services.conversation_lifecycle import ArchiveBlocked
    from src.services.archive.schemas import ArchiveFormatError

    try:
        async with archive_service() as service:
            org = UUID(request.org_id)
            if request.archive_id is None:
                raise ArchiveBlocked("archive_identity_required")
            archive = UUID(request.archive_id)
            if await service.resume_existing(org, archive):
                return
            if settings.ARCHIVE_ENABLED:
                if request.audit:
                    await service.run_audit(
                        org, apply=True, batch_size=settings.ARCHIVE_BATCH_SIZE, archive_id=archive
                    )
                elif request.conversation_id is not None:
                    await service.run(
                        org, UUID(request.conversation_id), apply=True, archive_id=archive
                    )
    except (ArchiveBlocked, ArchiveFormatError):
        raise ApplicationError("archive_blocked", non_retryable=True) from None
    except Exception:
        raise ApplicationError("archive_unavailable") from None


async def dispatch_data_batch(
    client: Client, *, sessions: SessionFactory = async_session, new_claims: bool = False
) -> int:
    from src.workflows.data_lifecycle import (
        DataLifecycleInput,
        DataLifecycleWorkflow,
        DATA_LIFECYCLE_QUEUE,
    )

    requests: list[DataLifecycleInput] = []
    async with sessions() as session:
        pending = (
            await session.execute(
                text("SELECT * FROM platform_archive_pending(:n)"),
                {"n": settings.ARCHIVE_BATCH_SIZE},
            )
        ).all()
        requests.extend(
            DataLifecycleInput(str(org), archive_id=str(archive)) for org, archive in pending
        )
        if new_claims:
            cutoff = datetime.now(timezone.utc) - timedelta(days=settings.ARCHIVE_RETENTION_DAYS)
            candidates = (
                await session.execute(
                    text("SELECT * FROM platform_archive_discover(:n,:cutoff)"),
                    {"n": settings.ARCHIVE_BATCH_SIZE, "cutoff": cutoff},
                )
            ).all()
            requests.extend(
                DataLifecycleInput(
                    str(org), conversation_id=str(conversation), archive_id=str(uuid4())
                )
                for org, conversation in candidates
            )
            organizations = (
                (
                    await session.execute(
                        text("SELECT * FROM platform_archive_audit_orgs(:n,:cutoff)"),
                        {"n": settings.ARCHIVE_BATCH_SIZE, "cutoff": cutoff},
                    )
                )
                .scalars()
                .all()
            )
            requests.extend(
                DataLifecycleInput(str(org), audit=True, archive_id=str(uuid4()))
                for org in organizations
            )
        await session.commit()
    for request in requests:
        identity = request.archive_id or request.conversation_id or request.org_id
        try:
            await client.start_workflow(
                DataLifecycleWorkflow.run,
                request,
                id=f"data-lifecycle:{identity}",
                task_queue=DATA_LIFECYCLE_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                execution_timeout=timedelta(minutes=35),
            )
        except WorkflowAlreadyStartedError:
            pass
    return len(requests)


async def _data_observer_loop(client: Client) -> None:
    from src.services.execution_closure import observe_pending_executions

    while True:
        try:
            await observe_pending_executions(client, async_session)
        except Exception:
            logger.warning("data_observation_unavailable")
        await asyncio.sleep(60)


async def run_data_worker() -> None:
    from src.services.archive.runtime import archive_service
    from src.workflows.data_lifecycle import DataLifecycleWorkflow, DATA_LIFECYCLE_QUEUE

    DATA_HEARTBEAT_PATH.unlink(missing_ok=True)
    engine.echo = False
    engine.sync_engine.hide_parameters = True
    client = await Client.connect(settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE)
    worker = Worker(
        client,
        task_queue=DATA_LIFECYCLE_QUEUE,
        workflows=[DataLifecycleWorkflow],
        activities=[archive_data],
        max_concurrent_activities=2,
        max_concurrent_workflow_tasks=4,
    )
    last_sweep = -float("inf")
    last_partitions = -float("inf")
    observer = asyncio.create_task(_data_observer_loop(client))
    try:
        async with worker:
            while True:
                try:
                    now = time.monotonic()
                    if now - last_partitions >= 3600:
                        async with async_session() as session:
                            await session.execute(text("SELECT platform_maintain_partitions()"))
                            await session.execute(
                                text("SELECT platform_drop_empty_archive_partitions(:cutoff)"),
                                {
                                    "cutoff": datetime.now(timezone.utc)
                                    - timedelta(days=settings.ARCHIVE_RETENTION_DAYS)
                                },
                            )
                            await session.commit()
                        last_partitions = now
                    new_claims = (
                        settings.ARCHIVE_ENABLED
                        and now - last_sweep >= settings.ARCHIVE_SWEEP_INTERVAL_SECONDS
                    )
                    await dispatch_data_batch(client, new_claims=new_claims)
                    if new_claims:
                        last_sweep = now
                    async with archive_service() as service:
                        if (
                            not await service.storage.healthy()
                            or not await client.service_client.check_health()
                            or service.rag is None
                            or (
                                await service.rag.http.get(
                                    f"{service.rag.base_url}/health", timeout=5
                                )
                            ).status_code
                            != 200
                        ):
                            raise RuntimeError("data_prerequisite_unavailable")
                    temporary = DATA_HEARTBEAT_PATH.with_suffix(f".{os.getpid()}.tmp")
                    temporary.write_text(str(time.monotonic()))
                    temporary.chmod(0o600)
                    temporary.replace(DATA_HEARTBEAT_PATH)
                except Exception:
                    logger.warning("data_maintenance_unavailable")
                await asyncio.sleep(60)
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        DATA_HEARTBEAT_PATH.unlink(missing_ok=True)
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Platform maintenance worker")
    parser.add_argument("role", choices=["identity", "identity-health", "data", "data-health"])
    args = parser.parse_args()
    if args.role == "identity-health":
        raise SystemExit(0 if identity_healthy() else 1)
    if args.role == "data-health":
        raise SystemExit(0 if data_healthy() else 1)
    if args.role == "data":
        logging.basicConfig(level=logging.INFO)
        asyncio.run(run_data_worker())
        return
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_identity_worker())


if __name__ == "__main__":
    main()
