"""Upgrade the actual queued legacy input, retaining its incomplete archive proof."""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer, Worker

from src.workflows.index_document import IndexDocumentInput, IndexDocumentWorkflow
from tests.legacy_index_workflow import LegacyWorkflow


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"),
        reason="dedicated live RAG/Temporal fixture required",
    ),
]


async def migrate(database, revision):
    service = Path(__file__).resolve().parents[2] / "rag-api"
    url = f"postgresql+asyncpg://postgres:postgres@127.0.0.1:55434/{database}"
    proc = await asyncio.create_subprocess_exec(
        str(service / ".venv/bin/alembic"),
        "upgrade",
        revision,
        cwd=service,
        env={**os.environ, "RAG_API_DATABASE_URL": url, "RAG_API_MIGRATION_DATABASE_URL": url},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await proc.wait() == 0, "isolated RAG schema migration failed"


@pytest.mark.parametrize("scenario", ["queued_text", "queued_source", "old_history"])
async def test_legacy_upgrade_retains_fences_and_incomplete_proof(monkeypatch, scenario):
    from src import activities, milvus_store
    from src.config import settings
    from src.indexing import ingestion
    from src.lifecycle import begin_vector, current_scope
    from pymilvus import MilvusClient
    from unittest.mock import AsyncMock

    database = "rag_upgrade_" + uuid4().hex
    org, kb, document = (uuid4() for _ in range(3))
    queue = "rag-legacy-upgrade-" + uuid4().hex
    workflow_id = f"rag-index-{document}"
    collection = "legacy_upgrade_" + uuid4().hex
    admin = await asyncpg.connect("postgresql://postgres:postgres@127.0.0.1:55434/postgres")
    await admin.execute(f"CREATE DATABASE {database}")
    await migrate(database, "0004")
    db = await asyncpg.connect(f"postgresql://postgres:postgres@127.0.0.1:55434/{database}")
    source = f"documents/{org}/{uuid4()}.txt" if scenario == "queued_source" else None
    await db.execute(
        "INSERT INTO documents(id,org_id,knowledge_base_id,content,source_key,source_filename,status) VALUES($1,$2,$3,$4,$5,'fixture.txt','pending')",
        document,
        org,
        kb,
        None if source else "Legacy fixture text.",
        source,
    )
    client = await Client.connect("127.0.0.1:57233")
    # No worker has ever polled this random queue. The stored input really has
    # only the old three fields; no operation, generation or replacement run.
    input = dict(document_id=str(document), org_id=str(org), knowledge_base_id=str(kb))
    handle = await client.start_workflow(
        "IndexDocumentWorkflow",
        input,
        id=workflow_id,
        task_queue=queue,
        execution_timeout=timedelta(seconds=60),
    )
    run = handle.first_execution_run_id
    journal_path = "/tmp/rag-worker-legacy-upgrade-journal.jsonl"
    with os.fdopen(
        os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a"
    ) as journal:
        journal.write(
            json.dumps(
                dict(
                    database=database,
                    org_id=str(org),
                    knowledge_base_id=str(kb),
                    document_id=str(document),
                    workflow_id=workflow_id,
                    run_id=run,
                    task_queue=queue,
                    collection=collection,
                    namespace="default",
                    scenario=scenario,
                )
            )
            + "\n"
        )
        journal.flush()
        os.fsync(journal.fileno())
    history = await handle.fetch_history()
    assert not any(
        event.HasField("workflow_task_started_event_attributes") for event in history.events
    )
    await migrate(database, "head")
    assert (
        await db.fetchval(
            "SELECT provenance_complete FROM rag_kb_lifecycle WHERE knowledge_base_id=$1", kb
        )
        is False
    )
    engine = create_async_engine(
        f"postgresql+asyncpg://rag_app:lifecycle-rag-password@127.0.0.1:55434/{database}",
        hide_parameters=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(activities, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(settings, "milvus_collection", collection)
    monkeypatch.setattr(settings, "embedding_dim", 2)
    vectors = MilvusClient(uri="http://127.0.0.1:59530", grpc_options={"grpc.enable_retries": 0})
    monkeypatch.setattr(milvus_store, "_client", vectors)
    # Keep the whole parsing/status/chunking/fenced vector path. Only external
    # source download and paid embedding/context requests use synthetic data.
    monkeypatch.setattr(
        "src.object_storage.object_storage.get_bytes",
        AsyncMock(return_value=b"Legacy fixture text."),
    )
    monkeypatch.setattr(ingestion, "generate_chunk_context", AsyncMock(return_value=""))
    monkeypatch.setattr(
        ingestion,
        "_embed_texts",
        AsyncMock(side_effect=lambda texts: [[0.25, -0.5] for _ in texts]),
    )
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[LegacyWorkflow if scenario == "old_history" else IndexDocumentWorkflow],
            activities=[
                activities.index_document,
                activities.register_index_execution,
                activities.mark_document_failed,
            ],
        ):
            await asyncio.wait_for(handle.result(), 75)
        assert await db.fetchval("SELECT status FROM documents WHERE id=$1", document) == "indexed"
        assert (
            await db.fetchval(
                "SELECT provenance_complete FROM rag_kb_lifecycle WHERE knowledge_base_id=$1", kb
            )
            is False
        )
        assert await db.fetchval("SELECT count(*) FROM rag_index_executions") == 0
        assert await db.fetchval("SELECT count(*) FROM rag_external_operations WHERE settled") > 0
        assert (
            await db.fetchval("SELECT count(*) FROM rag_external_operations WHERE NOT settled") == 0
        )
        assert vectors.query(
            collection_name=collection,
            filter=f'document_id == "{document}"',
            output_fields=["dense"],
            consistency_level="Strong",
        )[0]["dense"] == [0.25, -0.5]
        if source:
            assert (
                await db.fetchval("SELECT parsed_markdown FROM documents WHERE id=$1", document)
                == "Legacy fixture text."
            )
        history = await handle.fetch_history()
        await Replayer(workflows=[IndexDocumentWorkflow]).replay_workflow(history)
        # Legacy generation is still 1: restore-generation and uncertain external
        # operation guards must reject both status and vector mutations.
        scope = IndexDocumentInput(**input)
        await db.execute("UPDATE rag_kb_lifecycle SET generation=2 WHERE knowledge_base_id=$1", kb)
        for operation in (
            lambda: activities.index_document(scope),
            lambda: activities.mark_document_failed(
                str(document), str(org), str(kb), "synthetic_failure"
            ),
        ):
            with pytest.raises(Exception, match="rag_frozen"):
                await operation()
        token = current_scope.set(scope)
        try:
            with pytest.raises(Exception, match="rag_frozen"):
                await begin_vector(sessions)
            await db.execute(
                "UPDATE rag_kb_lifecycle SET generation=1 WHERE knowledge_base_id=$1", kb
            )
            await begin_vector(sessions)
            with pytest.raises(Exception, match="rag_external_uncertain"):
                await begin_vector(sessions)
        finally:
            current_scope.reset(token)
    finally:
        # Exact persisted run only. Missing history or a still-open execution
        # preserves DB/vector fixture and journal for recovery; never terminate.
        exact = client.get_workflow_handle(workflow_id, run_id=run)
        observed = await exact.describe()
        assert observed.run_id == run and observed.task_queue == queue
        assert observed.status != WorkflowExecutionStatus.RUNNING
        await exact.fetch_history()
        if vectors.has_collection(collection):
            vectors.drop_collection(collection)
        vectors.close()
        await engine.dispose()
        await db.close()
        await admin.execute(f"DROP DATABASE {database}")
        await admin.close()
