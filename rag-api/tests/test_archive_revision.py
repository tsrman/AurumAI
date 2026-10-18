"""Review regressions against real SQL and private HTTP commands."""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from src.config import settings
from src.archive.service import ArchiveService
from src.archive.config import ArchiveSettings
from src.lifecycle import Scope


@pytest.mark.asyncio
@pytest.mark.integration
async def test_restore_wire_rejects_archive_generation_before_any_work(monkeypatch):
    from src import main_archive

    org, kb, job = (uuid.uuid4() for _ in range(3))
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = ArchiveService(sessions, object(), object())
    verify = AsyncMock()
    monkeypatch.setattr(service, "_verify", verify)
    monkeypatch.setattr(main_archive, "ArchiveService", lambda *args: service)
    monkeypatch.setattr(main_archive, "MilvusClient", lambda **kwargs: object())
    monkeypatch.setattr(main_archive.boto3, "client", lambda *args, **kwargs: object())
    config = ArchiveSettings(
        archive_database_url=SecretStr(settings.database_url),
        archive_service_secret=SecretStr("fixture"),
        archive_s3_endpoint="http://fixture",
        archive_s3_access_key="fixture",
        archive_s3_secret_key=SecretStr("fixture"),
        archive_s3_bucket="archive",
        s3_endpoint="http://fixture",
        s3_access_key="fixture",
        s3_secret_key=SecretStr("fixture"),
        s3_bucket="source",
    )
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO rag_kb_lifecycle(org_id,knowledge_base_id,state,generation,archive_id) VALUES(:o,:k,'archived',2,:a)"
            ),
            dict(o=org, k=kb, a=job),
        )
        await db.execute(
            text(
                "INSERT INTO rag_archive_jobs(archive_id,org_id,knowledge_base_id,generation,state,inventory,manifest,receipt_hash) VALUES(:a,:o,:k,2,'purged','[]','{\"documents\":[]}',:h)"
            ),
            dict(o=org, k=kb, a=job, h="a" * 64),
        )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_archive.create_app(config)),
            base_url="http://private",
        ) as client:
            for dry in (True, False):
                response = await client.post(
                    "/internal/archive/restore",
                    headers={"X-Archive-Service-Secret": "fixture"},
                    json=dict(
                        org_id=str(org),
                        knowledge_base_id=str(kb),
                        archive_id=str(job),
                        generation=2,
                        dry_run=dry,
                    ),
                )
                assert response.status_code == 409
                assert response.json()["detail"] == "generation_conflict"
        verify.assert_not_awaited()
        assert (await service.status(Scope(org, kb, 2), job))["state"] == "purged"
    finally:
        async with sessions.begin() as db:
            await db.execute(text("DELETE FROM rag_archive_steps WHERE archive_id=:a"), dict(a=job))
            await db.execute(text("DELETE FROM rag_archive_jobs WHERE archive_id=:a"), dict(a=job))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("different_org", [False, True])
async def test_ordinary_api_rejects_foreign_canonical_source_before_admission(
    different_org, monkeypatch
):
    from src import main
    from fastapi import HTTPException

    org, kb, foreign = (uuid.uuid4() for _ in range(3))
    key = f"documents/{foreign if different_org else org}/{kb if different_org else foreign}/{uuid.uuid4()}.txt"

    def forbidden():
        raise AssertionError("foreign_source_reached_admission")

    monkeypatch.setattr(main, "AsyncSessionLocal", forbidden)
    with pytest.raises(HTTPException) as error:
        await main.create_document(
            main.DocumentCreate(title="fixture", source_key=key, knowledge_base_id=kb),
            str(org),
            str(kb),
        )
    assert error.value.status_code == 422
    assert error.value.detail == "rag_source_owner_invalid"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("pending", [False, True])
async def test_publish_requires_completion_receipts_even_when_data_matches(pending):
    from src.lifecycle import ArchiveBlocked

    org, kb, job = (uuid.uuid4() for _ in range(3))
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = ArchiveService(sessions, object(), object())
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO rag_kb_lifecycle(org_id,knowledge_base_id,state,generation,archive_id) VALUES(:o,:k,'restore_pending',3,:a)"
            ),
            dict(o=org, k=kb, a=job),
        )
        await db.execute(
            text(
                "INSERT INTO rag_archive_jobs(archive_id,org_id,knowledge_base_id,generation,restore_generation,state,inventory,manifest,receipt_hash) VALUES(:a,:o,:k,2,3,'restore_pending','[]','{\"documents\":[]}',:h)"
            ),
            dict(o=org, k=kb, a=job, h="a" * 64),
        )
        if pending:
            await db.execute(
                text(
                    "INSERT INTO rag_archive_steps VALUES(:a,:o,'sql_restore','documents','done'),(:a,:o,'source_restore','fixture','intent')"
                ),
                dict(a=job, o=org),
            )
    try:
        with pytest.raises(
            ArchiveBlocked,
            match="archive_external_uncertain" if pending else "archive_receipts_incomplete",
        ):
            await service.publish(Scope(org, kb, 3), job)
        assert (await service.status(Scope(org, kb, 3), job))["state"] == "restore_pending"
    finally:
        async with sessions.begin() as db:
            await db.execute(text("DELETE FROM rag_archive_steps WHERE archive_id=:a"), dict(a=job))
            await db.execute(text("DELETE FROM rag_archive_jobs WHERE archive_id=:a"), dict(a=job))
            await db.execute(
                text("DELETE FROM rag_kb_lifecycle WHERE knowledge_base_id=:k"), dict(k=kb)
            )
        await engine.dispose()
