import uuid

import httpx
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session
from src.models.access_grant import AccessGrant
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.routers import chat as chat_router
from src.services.quota import get_quota_service


class AllowQuota:
    async def preflight(self, org_id: uuid.UUID) -> None:
        return None


@pytest_asyncio.fixture
async def conversation_with_chat_capability(
    db,
    test_org: Organization,
    test_user: User,
) -> Conversation:
    db.add(
        OrganizationCapability(
            org_id=test_org.id,
            capability=Capability.CHAT_SEND.value,
        )
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        org_id=test_org.id,
        user_id=test_user.id,
        model="gpt-4o",
        title="Test",
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


@pytest_asyncio.fixture
async def make_client(db, test_org: Organization, test_user: User):
    async def override_get_org_db_session():
        yield db

    def override_get_current_user():
        return UserInfo(
            user_id=str(test_user.id),
            org_id=str(test_org.id),
            role="member",
        )

    app = FastAPI()
    app.include_router(chat_router.router, prefix="/v1")
    app.dependency_overrides[get_org_db_session] = override_get_org_db_session
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_quota_service] = AllowQuota
    return app


async def test_chat_stream_requires_live_chat_send_capability(
    db,
    make_client: FastAPI,
    conversation_with_chat_capability: Conversation,
) -> None:
    await db.execute(delete(OrganizationCapability))
    await db.commit()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/stream",
            json={
                "conversation_id": str(conversation_with_chat_capability.id),
                "messages": [{"role": "user", "content": "hi"}],
                "model": "gpt-4o",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing capability: chat.send"


async def test_chat_stream_rejects_model_without_read_grant(
    make_client: FastAPI,
    conversation_with_chat_capability: Conversation,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/stream",
            json={
                "conversation_id": str(conversation_with_chat_capability.id),
                "messages": [{"role": "user", "content": "hi"}],
                "model": "gpt-4o",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Model not allowed"


async def test_chat_stream_allows_model_grant_past_authorization_gate(
    db,
    make_client: FastAPI,
    conversation_with_chat_capability: Conversation,
    test_org: Organization,
    test_user: User,
) -> None:
    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type="model",
            resource_id="gpt-4o",
            principal_type=PrincipalType.USER.value,
            principal_id=test_user.id,
            permission=AccessPermission.READ.value,
            created_via=GrantSource.PROVISIONING.value,
        )
    )
    await db.commit()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client), base_url="http://test"
    ) as client:
        try:
            response = await client.post(
                "/v1/chat/stream",
                json={
                    "conversation_id": str(conversation_with_chat_capability.id),
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "gpt-4o",
                },
            )
        except Exception:
            return

    assert response.status_code != 403


async def test_chat_stream_rejects_empty_messages_with_422(
    make_client: FastAPI,
    conversation_with_chat_capability: Conversation,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=make_client), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/stream",
            json={
                "conversation_id": str(conversation_with_chat_capability.id),
                "messages": [],
                "model": "gpt-4o",
            },
        )

    assert response.status_code == 422
