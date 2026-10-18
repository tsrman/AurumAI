"""File upload behavior through POST /v1/documents/upload.

Replace only storage/rag-api boundaries to test admission, rejection and
downstream failure handling in the actual route.
"""

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user
from src.core.config import settings
from src.core.dependencies import get_org_db_session
from src.routers import documents as documents_router
from src.services import document_upload as document_upload_service

ORG_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
DOC_ID = str(uuid.uuid4())
KNOWLEDGE_BASE_ID = str(uuid.uuid4())


class FakeSession:
    async def scalar(self, statement):
        return None

    """Minimal AsyncSession stand-in that records what was added."""

    def __init__(self) -> None:
        self.added: list = []
        self.committed = False

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(fake_session):
    app = FastAPI()
    app.include_router(documents_router.router, prefix="/v1")

    async def fake_user() -> UserInfo:
        return UserInfo(
            user_id=USER_ID,
            org_id=ORG_ID,
            email="user@example.com",
            raw_claims={},
        )

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_org_db_session] = lambda: fake_session
    access = patch.object(
        documents_router,
        "_require_knowledge_access",
        AsyncMock(return_value=object()),
    )
    access.start()
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield client
    access.stop()


@pytest.fixture
def storage():
    put = AsyncMock()
    delete = AsyncMock()
    with patch.object(document_upload_service.object_storage, "put_bytes", put), patch.object(
        document_upload_service.object_storage, "delete", delete
    ):
        yield put, delete


@pytest.fixture
def rag_create():
    created = AsyncMock(
        return_value={
            "id": DOC_ID,
            "knowledge_base_id": KNOWLEDGE_BASE_ID,
            "title": "résumé",
            "source_filename": "résumé.pdf",
            "status": "pending",
            "chunk_count": 0,
            "created_at": "2026-08-29T00:00:00Z",
            "updated_at": "2026-08-29T00:00:00Z",
        }
    )
    with patch.object(document_upload_service.rag_client, "create_document", created):
        yield created


@pytest.mark.asyncio
async def test_pdf_is_stored_and_registered(client, storage, rag_create, fake_session):
    put, _ = storage

    async with client as c:
        response = await c.post(
            "/v1/documents/upload",
            data={"knowledge_base_id": KNOWLEDGE_BASE_ID},
            files={"file": ("résumé.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )

    assert response.status_code == 202
    assert response.json()["data"]["id"] == DOC_ID

    # Include org_id in object keys to separate tenant data in the bucket.
    key = put.await_args.args[0]
    assert key.startswith(f"documents/{ORG_ID}/{KNOWLEDGE_BASE_ID}/")
    assert key.endswith(".pdf")

    # Send rag-api the object key, not the source bytes.
    kwargs = rag_create.await_args.kwargs
    assert rag_create.await_args.args[:2] == (ORG_ID, KNOWLEDGE_BASE_ID)
    assert kwargs["source_key"] == key
    assert kwargs["source_filename"] == "résumé.pdf"
    assert "content" not in kwargs or kwargs["content"] is None

    # File metadata was persisted.
    assert fake_session.committed
    saved = fake_session.added[0]
    assert saved.filename == "résumé.pdf"
    assert saved.size_bytes == len(b"%PDF-1.4 fake")
    assert str(saved.document_id) == DOC_ID


@pytest.mark.asyncio
async def test_unsupported_extension_is_rejected(client, storage, rag_create):
    put, _ = storage

    async with client as c:
        response = await c.post(
            "/v1/documents/upload",
            data={"knowledge_base_id": KNOWLEDGE_BASE_ID},
            files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
        )

    assert response.status_code == 415
    put.assert_not_awaited()
    rag_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_over_the_limit_is_rejected_before_storage(
    client, storage, rag_create, monkeypatch
):
    """Check size before writing storage; otherwise the upload limit is ineffective."""
    put, _ = storage
    monkeypatch.setattr(settings, "DOCUMENT_UPLOAD_MAX_BYTES", 10)

    async with client as c:
        response = await c.post(
            "/v1/documents/upload",
            data={"knowledge_base_id": KNOWLEDGE_BASE_ID},
            files={"file": ("big.pdf", b"x" * 50, "application/pdf")},
        )

    assert response.status_code == 413
    put.assert_not_awaited()
    rag_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_file_is_rejected(client, storage, rag_create):
    put, _ = storage

    async with client as c:
        response = await c.post(
            "/v1/documents/upload",
            data={"knowledge_base_id": KNOWLEDGE_BASE_ID},
            files={"file": ("empty.md", b"", "text/markdown")},
        )

    assert response.status_code == 422
    put.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_rag_reply_preserves_source_object(client, storage, fake_session):
    """Avoid accumulating orphan objects with no document reference."""
    put, delete = storage

    with patch.object(
        document_upload_service.rag_client,
        "create_document",
        AsyncMock(side_effect=RuntimeError("rag-api unavailable")),
    ):
        async with client as c:
            with pytest.raises(RuntimeError):
                await c.post(
                    "/v1/documents/upload",
                    data={"knowledge_base_id": KNOWLEDGE_BASE_ID},
                    files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
                )

    put.assert_awaited_once()
    delete.assert_not_awaited()
    assert fake_session.added == []


@pytest.mark.asyncio
async def test_directory_traversal_in_filename_does_not_reach_the_key(
    client, storage, rag_create
):
    put, _ = storage

    async with client as c:
        response = await c.post(
            "/v1/documents/upload",
            data={"knowledge_base_id": KNOWLEDGE_BASE_ID},
            files={"file": ("../../etc/passwd.md", "# Überschrift".encode(), "text/markdown")},
        )

    assert response.status_code == 202
    key = put.await_args.args[0]
    assert ".." not in key
    assert rag_create.await_args.kwargs["source_filename"] == "passwd.md"
