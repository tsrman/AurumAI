import uuid
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.access_grant import AccessGrant
from src.models.knowledge_base import KnowledgeBase
from src.models.organization import Organization
from src.models.user import User
from src.routers import documents


def _grant(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    permission: AccessPermission,
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type="knowledge_base",
        resource_id=str(knowledge_base_id),
        principal_type=PrincipalType.USER.value,
        principal_id=user_id,
        permission=permission.value,
        created_via=GrantSource.PROVISIONING.value,
    )


def _app(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    session_token: bool = False,
) -> FastAPI:
    identity = UserInfo(
        user_id=str(user.id),
        org_id=str(org.id),
        role="member",
        raw_claims={"type": "session"} if session_token else {},
    )

    async def db_override():
        yield db

    app = FastAPI()
    app.include_router(documents.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_current_user_or_session] = lambda: identity
    app.dependency_overrides[get_org_db_session] = db_override
    app.dependency_overrides[get_service_db_session] = db_override
    return app


async def test_v2_document_list_checks_base_before_forwarding_trusted_scope(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    readable = KnowledgeBase(org_id=test_org.id, name="Readable")
    private = KnowledgeBase(org_id=test_org.id, name="Private")
    db.add_all([readable, private])
    await db.flush()
    db.add(_grant(test_org.id, test_user.id, readable.id, AccessPermission.READ))
    await db.commit()
    app = _app(db, test_org, test_user)

    with patch.object(
        documents.rag_client,
        "list_documents",
        AsyncMock(return_value=[]),
    ) as rag_list:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            allowed = await client.get(
                "/v1/documents",
                params={"knowledge_base_id": str(readable.id)},
            )
            denied = await client.get(
                "/v1/documents",
                params={"knowledge_base_id": str(private.id)},
            )

    assert allowed.status_code == 200
    assert denied.status_code == 404
    assert denied.json()["detail"] == "Knowledge base not found"
    rag_list.assert_awaited_once_with(str(test_org.id), str(readable.id))


async def test_v2_document_create_requires_write_not_only_read(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    knowledge_base = KnowledgeBase(org_id=test_org.id, name="Upload target")
    db.add(knowledge_base)
    await db.flush()
    grant = _grant(test_org.id, test_user.id, knowledge_base.id, AccessPermission.READ)
    db.add(grant)
    await db.commit()
    app = _app(db, test_org, test_user)
    rag_response = {
        "id": str(uuid.uuid4()),
        "knowledge_base_id": str(knowledge_base.id),
        "title": "Policy",
        "status": "pending",
        "chunk_count": 0,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }

    with patch.object(
        documents.rag_client,
        "create_document",
        AsyncMock(return_value=rag_response),
    ) as rag_create:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                "/v1/documents",
                json={
                    "knowledge_base_id": str(knowledge_base.id),
                    "title": "Policy",
                    "content": "text",
                },
            )
            grant.permission = AccessPermission.WRITE.value
            await db.commit()
            allowed = await client.post(
                "/v1/documents",
                json={
                    "knowledge_base_id": str(knowledge_base.id),
                    "title": "Policy",
                    "content": "text",
                },
            )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Knowledge base write access required"
    assert allowed.status_code == 202
    rag_create.assert_awaited_once_with(
        str(test_org.id),
        str(knowledge_base.id),
        "Policy",
        "text",
    )


async def test_internal_search_applies_live_revoke_before_rag_call(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    knowledge_base = KnowledgeBase(org_id=test_org.id, name="Search target")
    db.add(knowledge_base)
    await db.flush()
    grant = _grant(test_org.id, test_user.id, knowledge_base.id, AccessPermission.READ)
    db.add(grant)
    await db.commit()
    app = _app(db, test_org, test_user, session_token=True)

    with patch.object(
        documents.rag_client,
        "search",
        AsyncMock(return_value={"results": [], "warnings": []}),
    ) as rag_search:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            allowed = await client.post(
                f"/v1/knowledge-bases/{knowledge_base.id}/search",
                json={"query": "retention", "limit": 5},
            )
            await db.execute(delete(AccessGrant).where(AccessGrant.id == grant.id))
            await db.commit()
            revoked = await client.post(
                f"/v1/knowledge-bases/{knowledge_base.id}/search",
                json={"query": "retention", "limit": 5},
            )

    assert allowed.status_code == 200
    assert revoked.status_code == 404
    rag_search.assert_awaited_once_with(
        str(test_org.id),
        str(knowledge_base.id),
        query="retention",
        limit=5,
    )
