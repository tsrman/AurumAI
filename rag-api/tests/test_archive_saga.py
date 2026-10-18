"""Real PG/MinIO/Milvus archive roundtrip using exclusively owned UUIDs."""

import importlib.util
import logging
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.config import settings


@pytest.mark.skipif(
    not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"), reason="dedicated live RAG fixture required"
)
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "after_vector_done",
        "after_sql_commit",
        "after_source_done",
        "lost_source_reply",
        "corrupt_part",
        "missing_source",
        "source_collision",
        "schema_changed",
        "late_purge",
        "foreign_source",
        "shared_source",
        "sdk_source_delete",
        "sdk_source_restore",
        "sdk_vector_delete",
        "sdk_vector_restore",
        "sdk_cancel_restore",
        "sdk_cancel_inflight",
        "sdk_ordinary_delete",
    ],
)
async def test_real_archive_purge_restore_keeps_sources_and_exact_vectors(fault, monkeypatch):
    assert importlib.util.find_spec("src.archive.service") is not None
    import boto3
    from botocore.config import Config
    from pymilvus import MilvusClient
    from src.archive.service import ArchiveService
    from src.archive.storage import ArchiveStorage
    from src.archive.vector_snapshot import VectorSnapshot
    from src.lifecycle import ArchiveBlocked, Scope, claim
    from src import milvus_store

    logging.getLogger("pymilvus").disabled = True
    collection = "archive_test_" + uuid.uuid4().hex
    org, kb, document, job = (uuid.uuid4() for _ in range(4))
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = MilvusClient(uri="http://127.0.0.1:59530", grpc_options={"grpc.enable_retries": 0})
    old_client, old_collection, old_dim = (
        milvus_store._client,
        settings.milvus_collection,
        settings.embedding_dim,
    )
    milvus_store._client = client
    settings.milvus_collection = collection
    settings.embedding_dim = 2
    milvus_store.init_milvus_collection()
    milvus_store._client = old_client
    settings.milvus_collection = old_collection
    settings.embedding_dim = old_dim
    source = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:59000",
        aws_access_key_id="lifecycle-business",
        aws_secret_access_key="lifecycle-business-password",
        region_name="us-east-1",
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15),
    )
    archive = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:59000",
        aws_access_key_id="lifecycle-archives",
        aws_secret_access_key="lifecycle-archives-password",
        region_name="us-east-1",
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15),
    )
    # Administrative credentials are confined to fixture cleanup, never the
    # runtime ArchiveStorage: the archive IAM intentionally denies DeleteObject.
    cleanup_admin = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:59000",
        aws_access_key_id="lifecycle-minio",
        aws_secret_access_key="lifecycle-minio-password",
        region_name="us-east-1",
    )
    storage = ArchiveStorage(source, "lifecycle-business", archive, "lifecycle-archives")
    vectors = VectorSnapshot(client, collection)
    service = ArchiveService(sessions, storage, vectors)
    scope = Scope(org, kb, 2)
    key = f"documents/{org}/{kb}/{document}.txt"
    foreign_org, foreign_kb, foreign_doc = (uuid.uuid4() for _ in range(3))
    payload = b"original attachment\n" if fault != "none" else b"x" * (17 * 1024 * 1024)
    source.put_object(Bucket="lifecycle-business", Key=key, Body=payload)
    chunk_count = 129 if fault == "none" else 1
    now = datetime(2020, 2, 29, tzinfo=timezone.utc)
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO documents(id,org_id,knowledge_base_id,title,source_key,status,chunk_count,created_at,updated_at) VALUES(:d,:o,:k,'original',:s,'indexed',:n,:t,:t)"
            ),
            dict(d=document, o=org, k=kb, s=key, t=now, n=chunk_count),
        )
        # This storage-only fixture has no indexing workflow. The separate
        # real Temporal test verifies issuance of the same immutable receipt.
        await db.execute(
            text(
                "INSERT INTO rag_index_executions(operation_id,org_id,knowledge_base_id,document_id,generation,workflow_id,run_id,task_queue,terminal_status,closed_at) VALUES(:p,:o,:k,:d,1,:w,:r,'fixture','completed',now())"
            ),
            dict(
                p=uuid.uuid4(),
                o=org,
                k=kb,
                d=document,
                w="storage-fixture-" + str(document),
                r=uuid.uuid4(),
            ),
        )
        await db.commit()
    if fault in ("foreign_source", "shared_source"):
        async with sessions.begin() as db:
            # Existing ambiguous legacy reference is operator-seeded, never
            # admitted through the new ordinary ownership guard.
            owner_org, owner_kb = (
                (org, kb) if fault == "shared_source" else (foreign_org, foreign_kb)
            )
            await db.execute(
                text(
                    "INSERT INTO documents(id,org_id,knowledge_base_id,source_key) VALUES(:d,:o,:k,:s)"
                ),
                dict(d=foreign_doc, o=owner_org, k=owner_kb, s=key),
            )
            if fault == "shared_source":
                await db.execute(
                    text(
                        "INSERT INTO rag_index_executions(operation_id,org_id,knowledge_base_id,document_id,generation,workflow_id,run_id,task_queue,terminal_status,closed_at) VALUES(:p,:o,:k,:d,1,:w,:r,'fixture','completed',now())"
                    ),
                    dict(
                        p=uuid.uuid4(),
                        o=org,
                        k=kb,
                        d=foreign_doc,
                        w="storage-fixture-" + str(foreign_doc),
                        r=uuid.uuid4(),
                    ),
                )
    row = dict(
        pk=f"{document}:0",
        org_id=str(org),
        knowledge_base_id=str(kb),
        document_id=str(document),
        chunk_index=0,
        content="stored text",
        dense=[0.25, -0.5],
        parent_content="",
        acl_json='["public"]',
        document_title="original",
    )
    rows = [{**row, "pk": f"{document}:{i}", "chunk_index": i} for i in range(chunk_count)]
    client.upsert(collection_name=collection, data=rows)
    attempts = []
    import threading

    accepted_event, release_event, completed_event = (threading.Event() for _ in range(3))

    def inject_transport_loss():
        import asyncio
        import grpc
        from botocore.exceptions import ReadTimeoutError

        class AcceptedLost(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

            def details(self):
                return "fixture_accepted_reply_lost"

        def lose_reply():
            if fault == "sdk_cancel_restore":
                raise asyncio.CancelledError()
            raise AcceptedLost()

        if "source" in fault:
            send = source._endpoint.http_session.send
            mutation = "DELETE" if fault.endswith("delete") else "PUT"

            def accepted(request):
                response = send(request)
                if request.method == mutation:
                    attempts.append(mutation)
                    if len(attempts) == 1:
                        raise ReadTimeoutError(endpoint_url="fixture")
                return response

            monkeypatch.setattr(source._endpoint.http_session, "send", accepted)
        else:
            stub = client._get_connection()._stub
            if fault.endswith("delete"):
                original = stub.Delete.future

                def accepted_delete(*args, **kwargs):
                    future = original(*args, **kwargs)
                    future.result()
                    attempts.append(kwargs.get("timeout"))
                    if len(attempts) == 1:
                        lose_reply()
                    return future

                monkeypatch.setattr(stub.Delete, "future", accepted_delete)
            else:
                original = stub.Upsert

                def accepted_upsert(*args, **kwargs):
                    response = original(*args, **kwargs)
                    attempts.append(kwargs.get("timeout"))
                    if fault == "sdk_cancel_inflight":
                        accepted_event.set()
                        release_event.wait(30)
                        completed_event.set()
                        return response
                    if len(attempts) == 1:
                        lose_reply()
                    return response

                monkeypatch.setattr(stub, "Upsert", accepted_upsert)

    try:
        if fault == "sdk_ordinary_delete":
            from src import main
            from fastapi import HTTPException

            monkeypatch.setattr(main, "AsyncSessionLocal", sessions)
            monkeypatch.setattr(main, "temporal_client", object())
            monkeypatch.setattr(milvus_store, "_client", client)
            monkeypatch.setattr(settings, "milvus_collection", collection)
            inject_transport_loss()
            with pytest.raises(HTTPException) as error:
                await main.delete_document(str(document), str(org), str(kb))
            assert error.value.detail == "rag_vector_delete_unsettled"
            assert attempts == [15.0]
            with pytest.raises(ArchiveBlocked, match="external_operation_unsettled"):
                await claim(sessions, scope, job)
            async with sessions() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM rag_external_operations WHERE document_id=:d AND NOT settled"
                        ),
                        dict(d=document),
                    )
                    == 1
                )
                assert (
                    await db.scalar(
                        text("SELECT count(*) FROM documents WHERE id=:d"), dict(d=document)
                    )
                    == 1
                )
            return
        await claim(sessions, scope, job)
        if fault == "foreign_source":
            with pytest.raises(ArchiveBlocked, match="source_owner_unproven"):
                await service.export(scope, job)
            assert storage.read(key, source=True) == payload
            async with sessions() as db:
                assert (
                    await db.scalar(
                        text("SELECT count(*) FROM documents WHERE id=:d"), dict(d=foreign_doc)
                    )
                    == 1
                )
            return
        if fault == "missing_source":
            source.delete_object(Bucket="lifecycle-business", Key=key)
            with pytest.raises(ArchiveBlocked, match="source_missing"):
                await service.export(scope, job)
            async with sessions() as db:
                assert (
                    await db.scalar(
                        text("SELECT count(*) FROM documents WHERE id=:d"), dict(d=document)
                    )
                    == 1
                )
            return
        with pytest.raises(ArchiveBlocked):
            await service.export(Scope(uuid.uuid4(), kb, 2), job)
        exported = await service.export(scope, job)
        assert exported["state"] == "verified"
        assert len(exported["receipt_hash"]) == 64
        if fault in ("sdk_source_delete", "sdk_vector_delete"):
            inject_transport_loss()
            with pytest.raises(Exception):
                await service.purge(scope, job)
            assert len(attempts) == 1
            assert (await service.status(scope, job))["pending_steps"]
            with pytest.raises(ArchiveBlocked, match="archive_external_uncertain"):
                await service.purge(scope, job)
            with pytest.raises(ArchiveBlocked):
                await service.restore(Scope(org, kb, 3), job, dry_run=False)
            with pytest.raises(ArchiveBlocked):
                await service.publish(scope, job)
            assert len(attempts) == 1
            return
        if fault == "corrupt_part":
            async with sessions() as db:
                manifest = await db.scalar(
                    text("SELECT manifest FROM rag_archive_jobs WHERE archive_id=:a"), dict(a=job)
                )
            part = manifest["documents"][0]["metadata"]
            original = storage.read(part["key"])
            archive.put_object(Bucket="lifecycle-archives", Key=part["key"], Body=b"corrupt")
            with pytest.raises(ArchiveBlocked, match="artifact_corrupt"):
                await service.purge(scope, job)
            async with sessions() as db:
                assert (
                    await db.scalar(
                        text("SELECT count(*) FROM documents WHERE id=:d"), dict(d=document)
                    )
                    == 1
                )
            archive.put_object(Bucket="lifecycle-archives", Key=part["key"], Body=original)
        elif fault == "lost_source_reply":
            original_delete = storage.delete_source

            def lost_reply(key):
                original_delete(key)
                raise RuntimeError("injected_lost_reply")

            monkeypatch.setattr(storage, "delete_source", lost_reply)
            with pytest.raises(RuntimeError, match="injected_lost_reply"):
                await service.purge(scope, job)
            monkeypatch.setattr(storage, "delete_source", original_delete)
            with pytest.raises(ArchiveBlocked, match="archive_external_uncertain"):
                await service.purge(scope, job)
            with pytest.raises(ArchiveBlocked):
                await service.restore(Scope(org, kb, 3), job, dry_run=False)
            assert storage.read(key, source=True, missing_ok=True) is None
            return
        elif fault in ("after_vector_done", "after_sql_commit", "after_source_done"):
            original_step = service._external_step
            fired = False

            async def interrupted(scope, archive_id, step, resource, call):
                nonlocal fired
                if not fired and fault == "after_sql_commit" and step == "source_delete":
                    fired = True
                    raise RuntimeError("injected_crash")
                await original_step(scope, archive_id, step, resource, call)
                if not fired and (
                    (fault == "after_vector_done" and step == "vector_delete")
                    or (fault == "after_source_done" and step == "source_delete")
                ):
                    fired = True
                    raise RuntimeError("injected_crash")

            monkeypatch.setattr(service, "_external_step", interrupted)
            with pytest.raises(RuntimeError, match="injected_crash"):
                await service.purge(scope, job)
            monkeypatch.setattr(service, "_external_step", original_step)
        delayed = None
        if fault == "late_purge":
            import asyncio

            paused, resume = asyncio.Event(), asyncio.Event()
            finish = service._finish_purge

            async def pause_first(current_scope, archive_id):
                if not paused.is_set():
                    paused.set()
                    await resume.wait()
                await finish(current_scope, archive_id)

            monkeypatch.setattr(service, "_finish_purge", pause_first)
            delayed = asyncio.create_task(service.purge(scope, job))
            await asyncio.wait_for(paused.wait(), 30)
        await service.purge(scope, job)
        async with sessions() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM documents WHERE id=:d"), dict(d=document)
                )
                == 0
            )
        assert storage.read(key, source=True, missing_ok=True) is None
        assert list(vectors.rows(scope, document, 2)) == []
        restore_scope = Scope(org, kb, 3)
        if fault in (
            "sdk_source_restore",
            "sdk_vector_restore",
            "sdk_cancel_restore",
            "sdk_cancel_inflight",
        ):
            import asyncio

            inject_transport_loss()
            if fault == "sdk_cancel_inflight":
                running = asyncio.create_task(service.restore(restore_scope, job, dry_run=False))
                assert await asyncio.to_thread(accepted_event.wait, 30)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
                # The original accepted thread is still in flight here.
                assert not completed_event.is_set()
            else:
                with pytest.raises(
                    asyncio.CancelledError if fault == "sdk_cancel_restore" else Exception
                ):
                    await service.restore(restore_scope, job, dry_run=False)
            assert len(attempts) == 1
            assert (await service.status(restore_scope, job))["pending_steps"]
            with pytest.raises(ArchiveBlocked, match="archive_external_uncertain"):
                await service.restore(restore_scope, job, dry_run=False)
            with pytest.raises(ArchiveBlocked):
                await service.publish(restore_scope, job)
            with pytest.raises(ArchiveBlocked):
                await service.purge(scope, job)
            assert len(attempts) == 1
            if fault == "sdk_cancel_inflight":
                release_event.set()
                assert await asyncio.to_thread(completed_event.wait, 30)
                assert (await service.status(restore_scope, job))["pending_steps"]
            return
        for dry_run in (True, False):
            with pytest.raises(ArchiveBlocked, match="generation_conflict"):
                await service.restore(scope, job, dry_run=dry_run)
        if fault == "source_collision":
            source.put_object(Bucket="lifecycle-business", Key=key, Body=b"foreign content")
            with pytest.raises(ArchiveBlocked, match="restore_source_collision"):
                await service.restore(restore_scope, job, dry_run=True)
            assert storage.read(key, source=True) == b"foreign content"
            return
        if fault == "schema_changed":
            alternate = "archive_wrong_schema_" + uuid.uuid4().hex
            saved = milvus_store._client, settings.milvus_collection, settings.embedding_dim
            milvus_store._client = client
            settings.milvus_collection = alternate
            settings.embedding_dim = 3
            try:
                milvus_store.init_milvus_collection()
            finally:
                milvus_store._client, settings.milvus_collection, settings.embedding_dim = saved
            vectors.collection = alternate
            try:
                with pytest.raises(ArchiveBlocked, match="vector_schema_changed"):
                    await service.restore(restore_scope, job, dry_run=True)
            finally:
                vectors.collection = collection
                client.drop_collection(alternate)
            return
        dry = await service.restore(restore_scope, job, dry_run=True)
        assert dry["generation"] == 3 and dry["archive_generation"] == 2
        restored = await service.restore(restore_scope, job, dry_run=False)
        assert restored["state"] == "restore_pending"
        import hashlib

        assert storage.digest(key, source=True) == (
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )
        actual = [r for batch in vectors.rows(scope, document, 2) for r in batch]
        assert sorted(actual, key=lambda r: r["pk"]) == sorted(rows, key=lambda r: r["pk"])
        await service.publish(restore_scope, job)
        if delayed is not None:
            resume.set()
            with pytest.raises(ArchiveBlocked, match="generation_conflict"):
                await delayed
            assert (await service.status(restore_scope, job))["state"] == "restored"
        assert (await service.status(restore_scope, job))["receipt_hash"] == exported[
            "receipt_hash"
        ]
        async with sessions() as db:
            restored_row = (
                await db.execute(
                    text("SELECT title,created_at,status FROM documents WHERE id=:d"),
                    dict(d=document),
                )
            ).one()
            assert tuple(restored_row) == ("original", now, "indexed")
        assert (await service.restore(restore_scope, job, dry_run=False))["state"] == "restored"
    finally:
        release_event.set()
        for suffix in (
            "manifest.json",
            f"documents/{document}.parquet",
            f"sources/{document}",
            f"vectors/{document}/0.parquet",
            f"vectors/{document}/1.parquet",
            f"documents/{foreign_doc}.parquet",
            f"sources/{foreign_doc}",
        ):
            cleanup_admin.delete_object(
                Bucket="lifecycle-archives", Key=f"archives/v1/{org}/{job}/rag/{suffix}"
            )
        client.drop_collection(collection)
        source.delete_object(Bucket="lifecycle-business", Key=key)
        async with sessions() as db:
            for table in ("rag_archive_steps", "rag_archive_jobs"):
                await db.execute(text(f"DELETE FROM {table} WHERE archive_id=:a"), dict(a=job))
            await db.execute(
                text("DELETE FROM rag_index_executions WHERE document_id=:d"), dict(d=document)
            )
            await db.execute(
                text("DELETE FROM rag_external_operations WHERE document_id=:d"), dict(d=document)
            )
            await db.execute(
                text("DELETE FROM rag_index_executions WHERE document_id=:d"), dict(d=foreign_doc)
            )
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=document))
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=foreign_doc))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=foreign_kb)
            )
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
            await db.commit()
        await engine.dispose()
