"""Exact Temporal histories on the dedicated owned server."""

import os
import uuid
from datetime import timedelta
import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker
from src.index_execution import verify_execution
from src.lifecycle import ArchiveBlocked


@workflow.defn(name="IndexDocumentWorkflow")
class IndexFixture:
    @workflow.run
    async def run(self, payload: dict) -> None:
        await workflow.sleep(timedelta(milliseconds=10))


@pytest.mark.skipif(
    not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated live RAG fixture required"
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_index_history_accepts_matching_closed_run_and_rejects_wrong_scope():
    client = await Client.connect("127.0.0.1:57233")
    org, kb, document, operation = (uuid.uuid4() for _ in range(4))
    queue = "archive-rag-test-" + uuid.uuid4().hex
    workflow_id = f"rag-index-{document}-{operation}"
    payload = dict(
        document_id=str(document),
        org_id=str(org),
        knowledge_base_id=str(kb),
        generation=1,
        operation_id=str(operation),
    )
    async with Worker(client, task_queue=queue, workflows=[IndexFixture]):
        handle = await client.start_workflow(
            "IndexDocumentWorkflow", payload, id=workflow_id, task_queue=queue
        )
        await handle.result()
    row = dict(
        operation_id=operation,
        org_id=org,
        knowledge_base_id=kb,
        document_id=document,
        generation=1,
        workflow_id=workflow_id,
        run_id=uuid.UUID(handle.first_execution_run_id),
        task_queue=queue,
    )
    import json

    with os.fdopen(
        os.open(
            "/tmp/rag-api-archive-temporal-journal.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        ),
        "a",
    ) as journal:
        journal.write(json.dumps(row, default=str) + "\n")
    result = await verify_execution(client, row)
    assert result == (row["run_id"], "completed")
    with pytest.raises(ArchiveBlocked, match="index_provenance_invalid"):
        await verify_execution(client, {**row, "org_id": uuid.uuid4()})
    with pytest.raises(ArchiveBlocked, match="index_history_unavailable"):
        await verify_execution(client, {**row, "run_id": uuid.uuid4()})


@pytest.mark.skipif(
    not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated live RAG fixture required"
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_reconciles_same_operation_without_duplicate_document(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from src.config import settings
    from src import main
    from src.index_execution import observe_scope, observe_batch
    from src.lifecycle import Scope, claim

    client = await Client.connect("127.0.0.1:57233")
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, kb, operation, job = (uuid.uuid4() for _ in range(4))
    queue = "archive-create-test-" + uuid.uuid4().hex
    monkeypatch.setattr(main, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(main, "temporal_client", client)
    monkeypatch.setattr(settings, "ingest_task_queue", queue)
    request = main.DocumentCreate(
        title="fixture", content="fixture", knowledge_base_id=kb, operation_id=operation
    )
    try:
        async with Worker(client, task_queue=queue, workflows=[IndexFixture]):
            first = await main.create_document(request, str(org), str(kb))
            async with sessions() as db:
                established = (
                    await db.execute(
                        text(
                            "SELECT workflow_id,run_id FROM rag_index_executions WHERE operation_id=:p"
                        ),
                        dict(p=operation),
                    )
                ).one()
            await client.get_workflow_handle(
                established.workflow_id, run_id=str(established.run_id)
            ).result()

            async def forbid_restart(*args, **kwargs):
                raise AssertionError("established_execution_must_not_restart")

            monkeypatch.setattr(client, "start_workflow", forbid_restart)
            second = await main.create_document(request, str(org), str(kb))
            async with sessions() as db:
                executions = (
                    (
                        await db.execute(
                            text("SELECT * FROM rag_index_executions WHERE org_id=:o"), dict(o=org)
                        )
                    )
                    .mappings()
                    .all()
                )
            import json

            with os.fdopen(
                os.open(
                    "/tmp/rag-api-archive-temporal-journal.jsonl",
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                ),
                "a",
            ) as journal:
                for execution in executions:
                    journal.write(json.dumps(dict(execution), default=str) + "\n")
            for execution in executions:
                await client.get_workflow_handle(
                    execution["workflow_id"], run_id=str(execution["run_id"])
                ).result()
            assert first["id"] == second["id"]
            # Lost start reply: verification selects one exact original run.
            # A changing latest lookup after that selection must not influence
            # registration (or be called at all).
            async with sessions.begin() as db:
                await db.execute(
                    text("UPDATE rag_index_executions SET run_id=NULL WHERE operation_id=:p"),
                    dict(p=operation),
                )
            from src import index_execution

            verify_original = index_execution.verify_execution
            get_handle = client.get_workflow_handle

            async def select_then_change_latest(*args, **kwargs):
                result = await verify_original(*args, **kwargs)

                def require_exact(workflow_id, *, run_id=None, **options):
                    if run_id is None:
                        raise AssertionError("second_latest_lookup_after_verification")
                    return get_handle(workflow_id, run_id=run_id, **options)

                monkeypatch.setattr(client, "get_workflow_handle", require_exact)
                return result

            monkeypatch.setattr(index_execution, "verify_execution", select_then_change_latest)
            third = await main.create_document(request, str(org), str(kb))
            assert third["id"] == first["id"]
            async with sessions() as db:
                assert str(
                    await db.scalar(
                        text("SELECT run_id FROM rag_index_executions WHERE operation_id=:p"),
                        dict(p=operation),
                    )
                ) == str(established.run_id)
            monkeypatch.setattr(index_execution, "verify_execution", verify_original)
            monkeypatch.setattr(client, "get_workflow_handle", get_handle)
        await observe_batch(client, sessions)
        async with sessions() as db:
            assert await db.scalar(
                text(
                    "SELECT closed_at IS NOT NULL FROM rag_index_executions WHERE operation_id=:p"
                ),
                dict(p=operation),
            )
        # No network access is required after a durable verified closure receipt.
        await observe_scope(object(), sessions, Scope(org, kb, 1))
        await claim(sessions, Scope(org, kb, 2), job)
    finally:
        async with sessions() as db:
            await db.execute(text("DELETE FROM rag_archive_jobs WHERE archive_id=:a"), dict(a=job))
            await db.execute(text("DELETE FROM rag_index_executions WHERE org_id=:o"), dict(o=org))
            await db.execute(text("DELETE FROM documents WHERE org_id=:o"), dict(o=org))
            await db.execute(text("DELETE FROM rag_kb_lifecycle WHERE org_id=:o"), dict(o=org))
            await db.commit()
        await engine.dispose()


@workflow.defn(name="IndexDocumentWorkflow")
class ContinueIndexFixture:
    @workflow.run
    async def run(self, payload: dict) -> None:
        if not workflow.info().continued_run_id:
            workflow.continue_as_new(payload)


@pytest.mark.skipif(
    not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated Temporal fixture"
)
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["continue", "reset"])
async def test_null_run_rejects_real_continuation_and_reset(kind):
    from temporalio.api.common.v1 import WorkflowExecution
    from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
    from temporalio.api.enums.v1 import EventType

    client = await Client.connect("127.0.0.1:57233")
    org, kb, document, operation = (uuid.uuid4() for _ in range(4))
    queue = "archive-chain-test-" + uuid.uuid4().hex
    row = dict(
        org_id=org,
        knowledge_base_id=kb,
        document_id=document,
        operation_id=operation,
        generation=1,
        workflow_id=f"rag-index-{document}-{operation}",
        run_id=None,
        task_queue=queue,
    )
    payload = {
        k: str(v) if isinstance(v, uuid.UUID) else v
        for k, v in row.items()
        if k in ("org_id", "knowledge_base_id", "document_id", "operation_id", "generation")
    }
    async with Worker(
        client,
        task_queue=queue,
        workflows=[ContinueIndexFixture if kind == "continue" else IndexFixture],
    ):
        handle = await client.start_workflow(
            "IndexDocumentWorkflow", payload, id=row["workflow_id"], task_queue=queue
        )
        await handle.result()
        if kind == "reset":
            history = await client.get_workflow_handle(
                row["workflow_id"], run_id=handle.first_execution_run_id
            ).fetch_history()
            finish = next(
                e.event_id
                for e in history.events
                if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            )
            reset = await client.workflow_service.reset_workflow_execution(
                ResetWorkflowExecutionRequest(
                    namespace=client.namespace,
                    workflow_execution=WorkflowExecution(
                        workflow_id=row["workflow_id"], run_id=handle.first_execution_run_id
                    ),
                    reason="exact-owned-archive-review-fixture",
                    workflow_task_finish_event_id=finish,
                    request_id=str(uuid.uuid4()),
                )
            )
            await client.get_workflow_handle(row["workflow_id"], run_id=reset.run_id).result()
        description = await client.get_workflow_handle(row["workflow_id"]).describe()
        import json

        with os.fdopen(
            os.open(
                "/tmp/rag-api-archive-temporal-journal.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            ),
            "a",
        ) as journal:
            journal.write(
                json.dumps(
                    {
                        **row,
                        "original_run_id": handle.first_execution_run_id,
                        "selected_run_id": description.run_id,
                        "fixture_kind": kind,
                    },
                    default=str,
                )
                + "\n"
            )
        with pytest.raises(ArchiveBlocked, match="index_history_unsupported"):
            await verify_execution(client, row)

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from src.config import settings
        from src.index_execution import observe_scope
        from src.lifecycle import Scope, claim

        engine = create_async_engine(settings.database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as db:
            await db.execute(
                text("INSERT INTO documents(id,org_id,knowledge_base_id) VALUES(:d,:o,:k)"),
                dict(d=document, o=org, k=kb),
            )
            await db.execute(
                text("SELECT rag_prepare_index(:p,:o,:k,1,:d,:w,:q)"),
                dict(p=operation, o=org, k=kb, d=document, w=row["workflow_id"], q=queue),
            )
        try:
            with pytest.raises(ArchiveBlocked, match="index_history_unsupported"):
                await observe_scope(client, sessions, Scope(org, kb, 1))
            async with sessions() as db:
                assert await db.scalar(
                    text(
                        "SELECT run_id IS NULL AND closed_at IS NULL FROM rag_index_executions WHERE operation_id=:p"
                    ),
                    dict(p=operation),
                )
            with pytest.raises(ArchiveBlocked, match="index_execution_unclosed"):
                await claim(sessions, Scope(org, kb, 2), uuid.uuid4())
        finally:
            async with sessions.begin() as db:
                await db.execute(
                    text("DELETE FROM rag_index_executions WHERE operation_id=:p"),
                    dict(p=operation),
                )
                await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=document))
                await db.execute(
                    text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
                )
            await engine.dispose()


@pytest.mark.parametrize(
    "field",
    [
        "cron_schedule",
        "parent_workflow_namespace",
        "parent_workflow_namespace_id",
        "parent_run",
        "root",
        "parent_initiated_event_id",
        "parent_initiated_event_version",
        "first_execution_run_id",
        "original_execution_run_id",
    ],
)
@pytest.mark.asyncio
async def test_unsupported_start_shape_never_returns_selected_identity(field):
    from types import SimpleNamespace
    from temporalio.api.history.v1 import HistoryEvent
    from temporalio.api.enums.v1 import EventType

    run = str(uuid.uuid4())
    event = HistoryEvent(event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED)
    start = event.workflow_execution_started_event_attributes
    start.first_execution_run_id = run
    start.original_execution_run_id = run
    start.attempt = 1
    if field == "parent_run":
        start.parent_workflow_execution.run_id = str(uuid.uuid4())
    elif field == "root":
        start.root_workflow_execution.run_id = run
    elif field.startswith("parent_initiated"):
        setattr(start, field, 1)
    else:
        setattr(start, field, "fixture")

    async def describe():
        return SimpleNamespace(run_id=run)

    async def events(**kwargs):
        yield event

    handle = SimpleNamespace(describe=describe, fetch_history_events=events)
    client = SimpleNamespace(
        namespace="default", get_workflow_handle=lambda *args, **kwargs: handle
    )
    with pytest.raises(ArchiveBlocked, match="index_history_unsupported"):
        await verify_execution(client, dict(workflow_id="fixture", run_id=None))


@workflow.defn(name="ArchiveParentFixture")
class ParentIndexFixture:
    @workflow.run
    async def run(self, payload: dict) -> None:
        await workflow.execute_child_workflow(
            "IndexDocumentWorkflow",
            payload,
            id=f"rag-index-{payload['document_id']}-{payload['operation_id']}",
        )


@pytest.mark.skipif(
    not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated Temporal fixture"
)
@pytest.mark.asyncio
async def test_real_child_index_history_is_not_standalone():
    client = await Client.connect("127.0.0.1:57233")
    org, kb, document, operation = (uuid.uuid4() for _ in range(4))
    queue = "archive-child-test-" + uuid.uuid4().hex
    payload = dict(
        org_id=str(org),
        knowledge_base_id=str(kb),
        document_id=str(document),
        operation_id=str(operation),
        generation=1,
    )
    workflow_id = f"rag-index-{document}-{operation}"
    async with Worker(client, task_queue=queue, workflows=[ParentIndexFixture, IndexFixture]):
        parent = await client.start_workflow(
            "ArchiveParentFixture",
            payload,
            id="archive-parent-" + uuid.uuid4().hex,
            task_queue=queue,
        )
        await parent.result()
        child = await client.get_workflow_handle(workflow_id).describe()
        row = dict(**payload, workflow_id=workflow_id, run_id=None, task_queue=queue)
        import json

        with os.fdopen(
            os.open(
                "/tmp/rag-api-archive-temporal-journal.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            ),
            "a",
        ) as journal:
            journal.write(
                json.dumps(
                    {
                        **row,
                        "parent_id": parent.id,
                        "parent_run_id": parent.first_execution_run_id,
                        "child_run_id": child.run_id,
                    }
                )
                + "\n"
            )
        with pytest.raises(ArchiveBlocked, match="index_history_unsupported"):
            await verify_execution(client, row)
