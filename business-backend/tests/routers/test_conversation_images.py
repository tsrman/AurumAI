import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.conversation import Conversation
from src.models.conversation_image import ConversationImage
from src.models.organization import Organization
from src.models.user import User
from src.routers import conversation_images

PNG = b"\x89PNG\r\n\x1a\nbytes"


class _Storage:
    def __init__(self) -> None:
        self.reads: list[str] = []

    async def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return PNG


def _app(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> FastAPI:
    app = FastAPI()
    app.include_router(conversation_images.router, prefix="/v1")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_org_db_session] = session_override
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(user_id), org_id=str(org_id), role="member"
    )
    return app


async def _conversation_with_image(
    db: AsyncSession, org: Organization, user: User, *, status: str = "active"
) -> tuple[uuid.UUID, uuid.UUID]:
    conversation = Conversation(org_id=org.id, user_id=user.id, model="m", status=status)
    db.add(conversation)
    await db.flush()
    image_id = uuid.uuid4()
    db.add(
        ConversationImage(
            id=image_id,
            org_id=org.id,
            conversation_id=conversation.id,
            origin="uploaded",
            storage_key=f"conversation-images/{org.id}/{conversation.id}/{image_id}.png",
            content_type="image/png",
            size_bytes=len(PNG),
        )
    )
    await db.commit()
    return conversation.id, image_id


@pytest.fixture
def storage(monkeypatch) -> _Storage:  # noqa: ANN001
    fake = _Storage()
    monkeypatch.setattr(conversation_images, "object_storage", fake)
    return fake


async def test_owner_gets_the_bytes_with_safe_headers(db, test_org, test_user, storage) -> None:  # noqa: ANN001
    conversation_id, image_id = await _conversation_with_image(db, test_org, test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org.id, test_user.id)), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/conversations/{conversation_id}/images/{image_id}/content")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"].startswith("private")


async def test_other_users_and_deleted_conversations_get_404(db, test_org, test_user, storage) -> None:  # noqa: ANN001
    conversation_id, image_id = await _conversation_with_image(db, test_org, test_user)
    stranger = User(keycloak_id=uuid.uuid4(), email=f"s-{uuid.uuid4().hex[:8]}@example.com", org_id=test_org.id)
    db.add(stranger)
    await db.commit()
    url = f"/v1/conversations/{conversation_id}/images/{image_id}/content"
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org.id, stranger.id)), base_url="http://test"
    ) as client:
        assert (await client.get(url)).status_code == 404

    deleted_id, deleted_image = await _conversation_with_image(db, test_org, test_user, status="deleted")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org.id, test_user.id)), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/conversations/{deleted_id}/images/{deleted_image}/content")
        unknown = await client.get(f"/v1/conversations/{conversation_id}/images/{uuid.uuid4()}/content")
    assert response.status_code == 404 and unknown.status_code == 404
    assert storage.reads == []


async def test_an_image_of_another_conversation_is_not_served_by_id(db, test_org, test_user, storage) -> None:  # noqa: ANN001
    first, _ = await _conversation_with_image(db, test_org, test_user)
    _, other_image = await _conversation_with_image(db, test_org, test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org.id, test_user.id)), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/conversations/{first}/images/{other_image}/content")
    assert response.status_code == 404 and storage.reads == []


async def test_images_cascade_with_their_conversation_identity(db, test_org, test_user, storage) -> None:  # noqa: ANN001
    conversation_id, image_id = await _conversation_with_image(db, test_org, test_user)
    await db.execute(
        text("DELETE FROM conversation_identities WHERE id = :id"), {"id": conversation_id}
    )
    await db.commit()
    assert await db.get(ConversationImage, image_id) is None
