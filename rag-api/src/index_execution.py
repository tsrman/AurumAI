"""Verify exact RAG workflow identity and closed history; never latest-ID cancellation."""

from typing import Any
from uuid import UUID
from temporalio.client import Client
from temporalio.api.enums.v1 import EventType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.lifecycle import ArchiveBlocked, Scope, tenant


async def verify_execution(client: Client, row: dict[str, Any]) -> tuple[UUID, str | None]:
    if row.get("namespace", "default") != client.namespace:
        raise ArchiveBlocked("index_namespace_mismatch")
    handle = client.get_workflow_handle(
        row["workflow_id"], run_id=str(row["run_id"]) if row["run_id"] else None
    )
    try:
        description = await handle.describe()
        run_id = description.run_id
        if row["run_id"] is not None and run_id != str(row["run_id"]):
            raise ArchiveBlocked("index_run_changed")
        exact = client.get_workflow_handle(row["workflow_id"], run_id=run_id)
        events: list[Any] = []
        size = 0
        async for event in exact.fetch_history_events(page_size=128):
            size += event.ByteSize()
            if len(events) >= 10000 or size > 8 * 1024 * 1024:
                raise ArchiveBlocked("index_history_limit")
            events.append(event)
        if not events or events[0].event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            raise ArchiveBlocked("index_history_missing")
        started = events[0].workflow_execution_started_event_attributes
        if (
            started.continued_execution_run_id
            or started.first_execution_run_id != run_id
            or started.original_execution_run_id != run_id
            or started.cron_schedule
            or started.HasField("retry_policy")
            or started.initiator != 0
            or started.attempt != 1
            or started.parent_workflow_namespace
            or started.parent_workflow_namespace_id
            or started.HasField("parent_workflow_execution")
            or started.parent_initiated_event_id
            or started.parent_initiated_event_version
            or started.HasField("root_workflow_execution")
            or started.first_workflow_task_backoff.seconds
            or started.first_workflow_task_backoff.nanos
        ):
            raise ArchiveBlocked("index_history_unsupported")
        expected = dict(
            document_id=str(row["document_id"]),
            org_id=str(row["org_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            generation=row["generation"],
            operation_id=str(row["operation_id"]),
        )
        payloads = await client.data_converter.decode(started.input.payloads)
        if (
            payloads != [expected]
            or started.workflow_type.name != "IndexDocumentWorkflow"
            or started.task_queue.name != row["task_queue"]
            or started.parent_workflow_execution.workflow_id
        ):
            raise ArchiveBlocked("index_provenance_invalid")
        forbidden = {
            EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED,
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
        }
        if any(e.event_type in forbidden for e in events):
            raise ArchiveBlocked("index_history_unsupported")
        terminal = {
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED: "completed",
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED: "failed",
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED: "cancelled",
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED: "terminated",
            EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT: "timed_out",
        }
        status = terminal.get(events[-1].event_type)
        return UUID(run_id), status
    except ArchiveBlocked:
        raise
    except Exception:
        raise ArchiveBlocked("index_history_unavailable") from None


async def observe_scope(
    client: Client, sessions: async_sessionmaker[AsyncSession], scope: Scope
) -> None:
    async with sessions() as db:
        await tenant(db, scope.org_id)
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM rag_index_executions WHERE org_id=:o AND knowledge_base_id=:k AND closed_at IS NULL ORDER BY operation_id LIMIT 10001"
                    ),
                    scope.params(),
                )
            )
            .mappings()
            .all()
        )
    if len(rows) > 10000:
        raise ArchiveBlocked("index_inventory_limit")
    for row in rows:
        result = await verify_execution(client, dict(row))
        if result[1] is None:
            continue
        run_id, status = result
        async with sessions() as db:
            await tenant(db, scope.org_id)
            await db.execute(
                text(
                    "UPDATE rag_index_executions SET run_id=:r,terminal_status=:s,closed_at=now() WHERE operation_id=:p AND org_id=:o AND knowledge_base_id=:k AND (run_id IS NULL OR run_id=:r)"
                ),
                {**scope.params(), "p": row["operation_id"], "r": run_id, "s": status},
            )
            await db.commit()


async def observe_batch(
    client: Client, sessions: async_sessionmaker[AsyncSession], cursor: UUID | None = None
) -> UUID | None:
    """Narrow operator discovery; every receipt write still uses tenant context."""
    async with sessions() as db:
        rows = (
            (
                await db.execute(
                    text("SELECT * FROM rag_pending_index_executions(:cursor)"), dict(cursor=cursor)
                )
            )
            .mappings()
            .all()
        )
    if not rows:
        return None
    for row in rows:
        try:
            result = await verify_execution(client, dict(row))
            if result[1] is None:
                continue
            run_id, status = result
            async with sessions() as db:
                await tenant(db, row["org_id"])
                await db.execute(
                    text(
                        "UPDATE rag_index_executions SET run_id=:r,terminal_status=:s,closed_at=now() WHERE operation_id=:p AND org_id=:o AND (run_id IS NULL OR run_id=:r) AND closed_at IS NULL"
                    ),
                    dict(r=run_id, s=status, p=row["operation_id"], o=row["org_id"]),
                )
                await db.commit()
        except ArchiveBlocked:
            # Exact IDs only; history bodies and provider messages stay private.
            import logging

            logging.getLogger(__name__).warning(
                "rag_index_receipt_blocked operation_id=%s", row["operation_id"]
            )
    return UUID(str(rows[-1]["operation_id"]))
