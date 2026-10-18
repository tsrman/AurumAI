import importlib.util
import os
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


def test_index_input_carries_generation_and_durable_operation():
    from src.workflows.index_document import IndexDocumentInput

    fields = IndexDocumentInput.model_fields
    assert "generation" in fields and "operation_id" in fields


def test_vector_writer_requires_index_scope():
    assert importlib.util.find_spec("src.lifecycle") is not None
    from src.lifecycle import current_scope

    assert current_scope.get() is None


@pytest.mark.skipif(
    not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated live RAG fixture required"
)
@pytest.mark.asyncio
async def test_real_worker_old_generation_cannot_change_status_or_start_vector_rpc(monkeypatch):
    from src import activities
    from src.lifecycle import current_scope, begin_vector
    from src.workflows.index_document import IndexDocumentInput

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/rag_platform_test"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, kb, document = (uuid.uuid4() for _ in range(3))
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO documents(id,org_id,knowledge_base_id,status) VALUES(:d,:o,:k,'indexed')"
            ),
            dict(d=document, o=org, k=kb),
        )
        await db.execute(
            text("UPDATE rag_kb_lifecycle SET generation=2 WHERE knowledge_base_id=:k"), dict(k=kb)
        )
        await db.commit()
    monkeypatch.setattr(activities, "AsyncSessionLocal", sessions)
    try:
        with pytest.raises(Exception, match="rag_frozen"):
            await activities.mark_document_failed(str(document), str(org), str(kb), "stale", 1)
        scope = IndexDocumentInput(
            document_id=str(document), org_id=str(org), knowledge_base_id=str(kb), generation=1
        )
        with pytest.raises(Exception, match="rag_frozen"):
            await activities.index_document(scope)
        token = current_scope.set(scope)
        try:
            with pytest.raises(Exception, match="rag_frozen"):
                await begin_vector(sessions)
        finally:
            current_scope.reset(token)
        async with sessions() as db:
            assert (
                await db.scalar(text("SELECT status FROM documents WHERE id=:d"), dict(d=document))
                == "indexed"
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM rag_external_operations WHERE document_id=:d"),
                    dict(d=document),
                )
                == 0
            )
    finally:
        async with sessions() as db:
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=document))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated live RAG fixture required"
)
@pytest.mark.asyncio
async def test_real_workflow_registers_before_vector_write_and_finishes_receipt(monkeypatch):
    import logging
    from types import SimpleNamespace
    from temporalio.client import Client
    from temporalio.worker import Worker
    from pymilvus import MilvusClient
    from src import activities, milvus_store
    from src.config import settings
    from src.workflows.index_document import IndexDocumentWorkflow

    logging.getLogger("pymilvus").disabled = True
    org, kb, document, operation = (uuid.uuid4() for _ in range(4))
    queue = "rag-worker-archive-test-" + uuid.uuid4().hex
    workflow_id = f"rag-index-{document}-{operation}"
    collection = "archive_worker_test_" + uuid.uuid4().hex
    admin = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/rag_platform_test"
    )
    runtime = create_async_engine(
        "postgresql+asyncpg://rag_app:lifecycle-rag-password@127.0.0.1:55434/rag_platform_test"
    )
    sessions = async_sessionmaker(runtime, expire_on_commit=False)
    monkeypatch.setattr(activities, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(settings, "milvus_collection", collection)
    monkeypatch.setattr(settings, "embedding_dim", 2)
    vectors = MilvusClient(uri="http://127.0.0.1:59530", grpc_options={"grpc.enable_retries": 0})
    monkeypatch.setattr(milvus_store, "_client", vectors)
    milvus_store.init_milvus_collection()
    async with admin.begin() as db:
        await db.execute(
            text(
                "INSERT INTO documents(id,org_id,knowledge_base_id,content,title,status) VALUES(:d,:o,:k,'fixture text','fixture','pending')"
            ),
            dict(d=document, o=org, k=kb),
        )
        await db.execute(
            text("SELECT rag_prepare_index(:p,:o,:k,1,:d,:w,:q)"),
            dict(p=operation, o=org, k=kb, d=document, w=workflow_id, q=queue),
        )

    async def deterministic_ingestion(parsed, **kwargs):
        # Only paid embedding/chunk construction is deterministic. Admission,
        # DB writes, vector upsert and Temporal are the production paths.
        chunk = SimpleNamespace(
            org_id=str(org),
            knowledge_base_id=str(kb),
            document_id=str(document),
            chunk_index=0,
            content="fixture text",
            parent_content="",
            acl=["public"],
            document_title="fixture",
            embedding=[0.25, -0.5],
        )
        async with admin.connect() as db:
            assert await db.scalar(
                text("SELECT run_id IS NOT NULL FROM rag_index_executions WHERE operation_id=:p"),
                dict(p=operation),
            )
        result = await milvus_store.bulk_insert_milvus([(f"{document}:0", chunk)])
        return {"status": "ok", "chunks_created": 1, "milvus": result}

    monkeypatch.setattr("src.indexing.ingestion.ingest_parsed_document", deterministic_ingestion)
    client = await Client.connect("127.0.0.1:57233")
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[IndexDocumentWorkflow],
            activities=[
                activities.register_index_execution,
                activities.index_document,
                activities.mark_document_failed,
            ],
        ):
            handle = await client.start_workflow(
                "IndexDocumentWorkflow",
                dict(
                    document_id=str(document),
                    org_id=str(org),
                    knowledge_base_id=str(kb),
                    generation=1,
                    operation_id=str(operation),
                ),
                id=workflow_id,
                task_queue=queue,
            )
            import json

            with os.fdopen(
                os.open(
                    "/tmp/rag-worker-archive-temporal-journal.jsonl",
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                ),
                "a",
            ) as journal:
                journal.write(
                    json.dumps(
                        dict(
                            workflow_id=workflow_id,
                            run_id=handle.first_execution_run_id,
                            operation_id=str(operation),
                            org_id=str(org),
                            knowledge_base_id=str(kb),
                            document_id=str(document),
                            task_queue=queue,
                            generation=1,
                        )
                    )
                    + "\n"
                )
            await handle.result()
        async with admin.connect() as db:
            assert (
                await db.scalar(text("SELECT status FROM documents WHERE id=:d"), dict(d=document))
                == "indexed"
            )
            assert (
                await db.scalar(
                    text("SELECT chunk_count FROM documents WHERE id=:d"), dict(d=document)
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM rag_external_operations WHERE document_id=:d AND NOT settled"
                    ),
                    dict(d=document),
                )
                == 0
            )
            assert (
                str(
                    await db.scalar(
                        text("SELECT run_id FROM rag_index_executions WHERE operation_id=:p"),
                        dict(p=operation),
                    )
                )
                == handle.first_execution_run_id
            )
        assert vectors.query(
            collection_name=collection,
            filter=f'document_id == "{document}"',
            output_fields=["dense"],
            consistency_level="Strong",
        )[0]["dense"] == [0.25, -0.5]
    finally:
        vectors.drop_collection(collection)
        async with admin.begin() as db:
            await db.execute(
                text("DELETE FROM rag_external_operations WHERE document_id=:d"), dict(d=document)
            )
            await db.execute(
                text("DELETE FROM rag_index_executions WHERE document_id=:d"), dict(d=document)
            )
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=document))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
        await runtime.dispose()
        await admin.dispose()


@pytest.mark.skipif(not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated RAG fixture")
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_worker_installed_transport_loss_cannot_settle_or_retry(monkeypatch, cancel):
    import asyncio
    import logging
    import grpc
    from types import SimpleNamespace
    from pymilvus import MilvusClient
    from src import activities, milvus_store
    from src.config import settings
    from src.lifecycle import current_scope
    from src.workflows.index_document import IndexDocumentInput

    logging.getLogger("pymilvus").disabled = True
    org, kb, document = (uuid.uuid4() for _ in range(3))
    collection = "archive_worker_transport_" + uuid.uuid4().hex
    admin = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/rag_platform_test"
    )
    runtime = create_async_engine(
        "postgresql+asyncpg://rag_app:lifecycle-rag-password@127.0.0.1:55434/rag_platform_test"
    )
    sessions = async_sessionmaker(runtime, expire_on_commit=False)
    monkeypatch.setattr(activities, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(settings, "milvus_collection", collection)
    monkeypatch.setattr(settings, "embedding_dim", 2)
    vectors = MilvusClient(uri="http://127.0.0.1:59530", grpc_options={"grpc.enable_retries": 0})
    monkeypatch.setattr(milvus_store, "_client", vectors)
    milvus_store.init_milvus_collection()
    async with admin.begin() as db:
        await db.execute(
            text("INSERT INTO documents(id,org_id,knowledge_base_id) VALUES(:d,:o,:k)"),
            dict(d=document, o=org, k=kb),
        )
    original = vectors._get_connection()._stub.Upsert
    attempts = []

    class Lost(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "fixture_accepted_reply_lost"

    def accepted(*args, **kwargs):
        response = original(*args, **kwargs)
        attempts.append(kwargs.get("timeout"))
        if len(attempts) == 1:
            if cancel:
                raise asyncio.CancelledError()
            raise Lost()
        return response

    monkeypatch.setattr(vectors._get_connection()._stub, "Upsert", accepted)
    scope = IndexDocumentInput(
        document_id=str(document),
        org_id=str(org),
        knowledge_base_id=str(kb),
        generation=1,
        operation_id=str(uuid.uuid4()),
    )
    token = current_scope.set(scope)
    chunk = SimpleNamespace(
        org_id=str(org),
        knowledge_base_id=str(kb),
        document_id=str(document),
        chunk_index=0,
        content="fixture",
        parent_content="",
        acl=["public"],
        document_title="fixture",
        embedding=[0.25, -0.5],
    )
    try:
        with pytest.raises(asyncio.CancelledError if cancel else Lost):
            await milvus_store.bulk_insert_milvus([(f"{document}:0", chunk)])
        assert attempts == [15.0]
        async with admin.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM rag_external_operations WHERE document_id=:d AND NOT settled"
                    ),
                    dict(d=document),
                )
                == 1
            )
        with pytest.raises(Exception, match="rag_external_uncertain"):
            await milvus_store.bulk_insert_milvus([(f"{document}:0", chunk)])
        assert attempts == [15.0]
    finally:
        current_scope.reset(token)
        vectors.drop_collection(collection)
        async with admin.begin() as db:
            await db.execute(
                text("DELETE FROM rag_external_operations WHERE document_id=:d"), dict(d=document)
            )
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=document))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
        await runtime.dispose()
        await admin.dispose()
