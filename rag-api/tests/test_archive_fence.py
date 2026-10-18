"""Real SQL writer/claim races and operator privilege boundary."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.config import settings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ordinary_writer_cannot_mutate_frozen_kb_or_forge_receipts():
    engine = create_async_engine(settings.database_url)
    org, kb, doc, job = (uuid.uuid4() for _ in range(4))
    try:
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT to_regclass('rag_kb_lifecycle')")) is not None
            await db.execute(
                text("INSERT INTO documents(id,org_id,knowledge_base_id) VALUES(:d,:o,:k)"),
                dict(d=doc, o=org, k=kb),
            )
            await db.execute(
                text(
                    "UPDATE rag_kb_lifecycle SET state='archiving',generation=2,archive_id=:j WHERE org_id=:o AND knowledge_base_id=:k"
                ),
                dict(j=job, o=org, k=kb),
            )
        async with engine.connect() as db:
            await db.execute(text("SET ROLE rag_app"))
            with pytest.raises(Exception, match="rag_frozen"):
                await db.execute(
                    text("UPDATE documents SET status='indexed' WHERE id=:d"), dict(d=doc)
                )
            await db.rollback()
            await db.execute(text("SET ROLE rag_app"))
            with pytest.raises(Exception, match="permission denied"):
                await db.execute(
                    text("UPDATE rag_kb_lifecycle SET state='active' WHERE knowledge_base_id=:k"),
                    dict(k=kb),
                )
            await db.rollback()
    finally:
        async with engine.begin() as db:
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=doc))
            if await db.scalar(text("SELECT to_regclass('rag_kb_lifecycle')")):
                await db.execute(
                    text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
                )
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vector_intent_survives_failure_and_prevents_claim():
    from sqlalchemy.ext.asyncio import async_sessionmaker
    import importlib

    assert importlib.util.find_spec("src.lifecycle") is not None
    from src.lifecycle import begin_external, claim, Scope, ArchiveBlocked

    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, kb, doc, job = (uuid.uuid4() for _ in range(4))
    async with sessions() as db:
        await db.execute(
            text("INSERT INTO documents(id,org_id,knowledge_base_id) VALUES(:d,:o,:k)"),
            dict(d=doc, o=org, k=kb),
        )
        await db.commit()
    scope = Scope(org, kb, 1)
    operation = await begin_external(sessions, scope, doc, "vector_upsert")
    assert operation is not None
    # No RPC completion: a new connection, closed Temporal, and elapsed lease
    # must never infer that a timed-out threaded write did not reach Milvus.
    with pytest.raises(ArchiveBlocked, match="external_operation_unsettled"):
        await claim(sessions, Scope(org, kb, 2), job)
    async with sessions() as db:
        assert await db.scalar(
            text("SELECT NOT settled FROM rag_external_operations WHERE operation_id=:p"),
            dict(p=operation),
        )
        await db.execute(
            text("DELETE FROM rag_external_operations WHERE operation_id=:p"), dict(p=operation)
        )
        await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=doc))
        await db.execute(
            text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
        )
        await db.commit()
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_and_new_document_are_serialized_in_both_orders():
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.lifecycle import Scope, claim, ArchiveBlocked

    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, kb, document, job = (uuid.uuid4() for _ in range(4))
    async with sessions() as db:
        await db.execute(text("SELECT rag_writer_fence(:o,:k,1)"), dict(o=org, k=kb))
        await db.commit()
    try:
        async with sessions() as writer:
            await writer.execute(text("SET LOCAL ROLE rag_app"))
            await writer.execute(text("SELECT rag_writer_fence(:o,:k,1)"), dict(o=org, k=kb))
            pending = asyncio.create_task(claim(sessions, Scope(org, kb, 2), job))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 0.1)
            await writer.execute(
                text("INSERT INTO documents(id,org_id,knowledge_base_id) VALUES(:d,:o,:k)"),
                dict(d=document, o=org, k=kb),
            )
            await writer.commit()
            with pytest.raises(ArchiveBlocked, match="document_provenance_unknown"):
                await pending
        async with sessions() as db:
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=document))
            await db.commit()
        await claim(sessions, Scope(org, kb, 2), job)
        async with sessions() as writer:
            await writer.execute(text("SET LOCAL ROLE rag_app"))
            with pytest.raises(Exception, match="rag_frozen"):
                await writer.execute(
                    text("INSERT INTO documents(id,org_id,knowledge_base_id) VALUES(:d,:o,:k)"),
                    dict(d=document, o=org, k=kb),
                )
        async with sessions() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM documents WHERE id=:d"), dict(d=document)
                )
                == 0
            )
    finally:
        async with sessions() as db:
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=document))
            await db.execute(text("DELETE FROM rag_archive_jobs WHERE archive_id=:a"), dict(a=job))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_cannot_forge_manifest_closure_or_discover_other_tenants():
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as db:
            for sql in (
                "UPDATE rag_index_executions SET closed_at=now() WHERE false",
                "UPDATE rag_archive_jobs SET state='verified' WHERE false",
                "SELECT * FROM rag_pending_index_executions(NULL)",
            ):
                await db.execute(text("SET LOCAL ROLE rag_app"))
                with pytest.raises(Exception, match="permission denied"):
                    await db.execute(text(sql))
                await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_identity_is_canonical_and_immutable_for_ordinary_sql():
    engine = create_async_engine(settings.database_url)
    org, other_org, kb, other_kb, doc = (uuid.uuid4() for _ in range(5))
    own = f"documents/{org}/{kb}/{uuid.uuid4()}.txt"
    try:
        async with engine.begin() as db:
            await db.execute(text("SET LOCAL ROLE rag_app"))
            await db.execute(
                text(
                    "INSERT INTO documents(id,org_id,knowledge_base_id,source_key) VALUES(:d,:o,:k,:s)"
                ),
                dict(d=doc, o=org, k=kb, s=own),
            )
        for foreign in (
            f"documents/{org}/{other_kb}/{uuid.uuid4()}.txt",
            f"documents/{other_org}/{kb}/{uuid.uuid4()}.txt",
            f"documents/{org}/{kb}/{uuid.uuid4()}.txt",
        ):
            async with engine.connect() as db:
                await db.execute(text("SET ROLE rag_app"))
                with pytest.raises(Exception, match="rag_source_immutable"):
                    await db.execute(
                        text("UPDATE documents SET source_key=:s WHERE id=:d"),
                        dict(s=foreign, d=doc),
                    )
                await db.rollback()
        for foreign in (
            f"documents/{org}/{other_kb}/{uuid.uuid4()}.txt",
            f"documents/{other_org}/{kb}/{uuid.uuid4()}.txt",
            "legacy/shared.txt",
        ):
            async with engine.connect() as db:
                await db.execute(text("SET ROLE rag_app"))
                with pytest.raises(Exception, match="rag_source_owner_invalid"):
                    await db.execute(
                        text(
                            "INSERT INTO documents(id,org_id,knowledge_base_id,source_key) VALUES(:d,:o,:k,:s)"
                        ),
                        dict(d=uuid.uuid4(), o=org, k=kb, s=foreign),
                    )
                await db.rollback()
    finally:
        async with engine.begin() as db:
            await db.execute(text("DELETE FROM documents WHERE id=:d"), dict(d=doc))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
        await engine.dispose()
