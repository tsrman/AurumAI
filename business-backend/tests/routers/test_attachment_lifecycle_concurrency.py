"""Committed attachment intents replace advisory locks across external I/O."""

# ruff: noqa: F811
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.auth import UserInfo, get_current_user
from src.core.config import settings
from src.core.database import _AppSession
from src.core.dependencies import get_org_db_session
from src.models.access_grant import AccessGrant
from src.models.archive_control import ConversationOperation
from src.models.file_metadata import FileMetadata
from src.models.knowledge_base import KnowledgeBase
from src.routers import documents, knowledge_bases
from src.services import document_upload
from tests.services.test_interaction_outbox import outbox_fixture  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("lost_reply", [False, True])
async def test_hidden_kb_delete_cannot_pass_committed_attachment_intent(
    outbox_fixture, monkeypatch, upload, lost_reply
):
    fixture = outbox_fixture
    kb_id = uuid4()
    async with fixture.sessions.begin() as setup:
        setup.add(
            KnowledgeBase(
                id=kb_id,
                org_id=fixture.org_id,
                owning_conversation_id=fixture.conversation_id,
                name=str(kb_id),
            )
        )
        await setup.flush()
        setup.add(
            AccessGrant(
                org_id=fixture.org_id,
                resource_type="knowledge_base",
                resource_id=str(kb_id),
                principal_type="user",
                principal_id=fixture.user_id,
                permission="write",
                created_via="provisioning",
            )
        )
    engine = create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"role": "aurum_ai_app"}},
    )
    sessions = async_sessionmaker(engine, sync_session_class=_AppSession, expire_on_commit=False)

    async def org_session():
        async with sessions() as session:
            session.info["org_id"] = str(fixture.org_id)
            yield session

    app = FastAPI()
    app.include_router(documents.router, prefix="/v1")
    app.include_router(knowledge_bases.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(fixture.user_id), org_id=str(fixture.org_id), role="member"
    )
    app.dependency_overrides[get_org_db_session] = org_session
    entered, release = asyncio.Event(), asyncio.Event()
    remote_documents = []

    async def create_remote(*args, **kwargs):
        entered.set()
        await release.wait()
        now = datetime.now(timezone.utc).isoformat()
        result = {
            "id": str(uuid4()),
            "knowledge_base_id": str(kb_id),
            "title": "test",
            "status": "pending",
            "chunk_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        if lost_reply:
            # Acceptance is uncertain; a list reply cannot replace the pending intent.
            raise HTTPException(503, "external reply unavailable")
        remote_documents.append(result)
        return result

    monkeypatch.setattr(documents.rag_client, "create_document", create_remote)
    monkeypatch.setattr(
        knowledge_bases.rag_client,
        "list_documents",
        AsyncMock(side_effect=lambda *args: list(remote_documents)),
    )
    monkeypatch.setattr(document_upload.object_storage, "put_bytes", AsyncMock())
    task = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            request = (
                client.post(
                    "/v1/documents/upload",
                    data={"knowledge_base_id": str(kb_id)},
                    files={"file": ("test.txt", b"test", "text/plain")},
                )
                if upload
                else client.post(
                    "/v1/documents",
                    json={"knowledge_base_id": str(kb_id), "title": "test", "content": "test"},
                )
            )
            task = asyncio.create_task(request)
            await asyncio.wait_for(entered.wait(), 5)
            async with fixture.sessions() as observer:
                intent = await observer.scalar(
                    select(ConversationOperation).where(
                        ConversationOperation.knowledge_base_id == kb_id
                    )
                )
                assert intent is not None and intent.status == "pending"
                if upload:
                    assert intent.resource_key.startswith(f"documents/{fixture.org_id}/{kb_id}/")
            deletion = await asyncio.wait_for(client.delete(f"/v1/knowledge-bases/{kb_id}"), 3)
            assert deletion.status_code == 409
            async with fixture.sessions() as observer:
                assert await observer.get(KnowledgeBase, kb_id) is not None
            release.set()
            response = await asyncio.wait_for(task, 5)
            assert response.status_code == (503 if lost_reply else 202)
            if lost_reply:
                assert (await client.delete(f"/v1/knowledge-bases/{kb_id}")).status_code == 409
                async with fixture.sessions() as observer:
                    assert (
                        await observer.scalar(
                            select(ConversationOperation.status).where(
                                ConversationOperation.knowledge_base_id == kb_id
                            )
                        )
                        == "pending"
                    )
    finally:
        release.set()
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        await engine.dispose()
        async with fixture.sessions.begin() as cleanup:
            await cleanup.execute(
                delete(FileMetadata).where(
                    FileMetadata.owning_conversation_id == fixture.conversation_id
                )
            )
            await cleanup.execute(
                delete(ConversationOperation).where(
                    ConversationOperation.knowledge_base_id == kb_id
                )
            )
            await cleanup.execute(delete(AccessGrant).where(AccessGrant.resource_id == str(kb_id)))
            await cleanup.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
