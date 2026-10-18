import uuid
from unittest.mock import AsyncMock, patch

import pytest
from src.core.auth import UserInfo
from src.routers import documents as documents_router
from src.routers.documents import (
    create_document,
    delete_document,
    get_document,
    list_documents,
    search_knowledge_base,
)
from src.schemas.document import DocumentCreate, DocumentSearch


KNOWLEDGE_BASE_ID = uuid.uuid4()


@pytest.fixture
def test_user():
    return UserInfo(
        user_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), role="member"
    )

@pytest.mark.asyncio
async def test_create_document_calls_rag_client(test_user):
    data = DocumentCreate(
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        title="Test Doc",
        content="Test Content",
    )
    with (
        patch(
            "src.routers.documents._require_knowledge_access",
            new_callable=AsyncMock,
        ),
        patch(
            "src.routers.documents.rag_client.create_document",
            new_callable=AsyncMock,
        ) as mock_create,
    ):
        mock_create.return_value = {
            "id": str(uuid.uuid4()),
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "title": "Test Doc",
            "status": "pending",
            "chunk_count": 0,
            "created_at": "2026-07-18T22:00:00Z",
            "updated_at": "2026-07-18T22:00:00Z"
        }
        session = AsyncMock()
        session.scalar.return_value = None
        resp = await create_document(data, test_user, session)
        assert resp.data.title == "Test Doc"
        mock_create.assert_called_once_with(
            test_user.org_id,
            str(KNOWLEDGE_BASE_ID),
            "Test Doc",
            "Test Content",
        )

@pytest.mark.asyncio
async def test_list_documents_calls_rag_client(test_user):
    with (
        patch(
            "src.routers.documents._require_knowledge_access",
            new_callable=AsyncMock,
        ),
        patch(
            "src.routers.documents.rag_client.list_documents",
            new_callable=AsyncMock,
        ) as mock_list,
    ):
        mock_list.return_value = []
        resp = await list_documents(
            KNOWLEDGE_BASE_ID, test_user, AsyncMock()
        )
        assert resp.data == []
        mock_list.assert_called_once_with(test_user.org_id, str(KNOWLEDGE_BASE_ID))

@pytest.mark.asyncio
async def test_get_document_calls_rag_client(test_user):
    doc_id = str(uuid.uuid4())
    with (
        patch(
            "src.routers.documents._require_knowledge_access",
            new_callable=AsyncMock,
        ),
        patch(
            "src.routers.documents.rag_client.get_document",
            new_callable=AsyncMock,
        ) as mock_get,
    ):
        mock_get.return_value = {
            "id": doc_id,
            "knowledge_base_id": str(KNOWLEDGE_BASE_ID),
            "title": "Test Doc",
            "status": "indexed",
            "chunk_count": 5,
            "created_at": "2026-07-18T22:00:00Z",
            "updated_at": "2026-07-18T22:00:00Z"
        }
        resp = await get_document(
            doc_id, KNOWLEDGE_BASE_ID, test_user, AsyncMock()
        )
        assert str(resp.data.id) == doc_id
        mock_get.assert_called_once_with(
            test_user.org_id, str(KNOWLEDGE_BASE_ID), doc_id
        )

@pytest.mark.asyncio
async def test_delete_document_delegates_to_cleanup_service(test_user):
    doc_id = str(uuid.uuid4())
    with (
        patch(
            "src.routers.documents._require_knowledge_access",
            new_callable=AsyncMock,
        ),
        patch.object(
            documents_router.document_cleanup_service,
            "delete",
            new_callable=AsyncMock,
        ) as mock_delete,
    ):
        session = AsyncMock()
        await delete_document(
            doc_id, KNOWLEDGE_BASE_ID, test_user, session
        )
        mock_delete.assert_awaited_once_with(
            session,
            org_id=uuid.UUID(test_user.org_id),
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            document_id=doc_id,
        )


@pytest.mark.asyncio
async def test_search_replaces_internal_rag_warnings_with_safe_message(test_user):
    internal_result = {
        "results": [
            {
                "content": "owned result",
                "relevance_score": 0.25,
                "metadata": {"document_id": str(uuid.uuid4())},
            }
        ],
        "warnings": ["Milvus failed at tcp://10.0.0.7:19530: secret detail"],
    }
    with (
        patch(
            "src.routers.documents._require_knowledge_access",
            new_callable=AsyncMock,
        ),
        patch.object(
            documents_router.rag_client,
            "search",
            AsyncMock(return_value=internal_result),
        ),
    ):
        response = await search_knowledge_base(
            KNOWLEDGE_BASE_ID,
            DocumentSearch(query="owned", limit=10),
            test_user,
            AsyncMock(),
        )

    assert response == {
        "results": internal_result["results"],
        "warnings": ["Search completed with limitations"],
    }


@pytest.mark.asyncio
async def test_search_keeps_empty_warning_list_empty(test_user):
    with (
        patch(
            "src.routers.documents._require_knowledge_access",
            new_callable=AsyncMock,
        ),
        patch.object(
            documents_router.rag_client,
            "search",
            AsyncMock(return_value={"results": [], "warnings": []}),
        ),
    ):
        response = await search_knowledge_base(
            KNOWLEDGE_BASE_ID,
            DocumentSearch(query="missing", limit=10),
            test_user,
            AsyncMock(),
        )

    assert response == {"results": [], "warnings": []}
