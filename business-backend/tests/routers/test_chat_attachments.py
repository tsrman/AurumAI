import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.user import User
from src.routers import chat


@pytest_asyncio.fixture
async def client(db, test_org: Organization, test_user: User):
    async def override_get_org_db_session():
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(test_org.id)},
        )
        yield db
        await db.execute(text("SELECT set_config('app.current_org_id', '', false)"))

    def override_get_current_user():
        return UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")

    test_app = FastAPI()
    test_app.include_router(chat.router, prefix="/v1")
    test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session
    test_app.dependency_overrides[get_service_db_session] = override_get_org_db_session
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[get_current_user_or_session] = override_get_current_user

    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c


async def test_attach_file_to_own_conversation_succeeds(client, db, test_user, test_org):
    conv = Conversation(
        org_id=test_org.id, user_id=test_user.id, title="Mine", model="gpt-4o-mini"
    )
    db.add(conv)
    await db.commit()

    fake_doc = {
        "id": str(uuid.uuid4()),
        "knowledge_base_id": str(uuid.uuid4()),
        "title": "notes",
        "status": "pending",
        "chunk_count": 0,
        "created_at": "2026-09-04T00:00:00Z",
        "updated_at": "2026-09-04T00:00:00Z",
    }
    with patch("src.routers.chat.attach_file", AsyncMock(return_value=fake_doc)):
        response = await client.post(
            f"/v1/conversations/{conv.id}/attachments",
            files={"file": ("notes.md", b"# hello", "text/markdown")},
        )

    assert response.status_code == 202
    assert response.json()["data"]["id"] == fake_doc["id"]


async def test_attach_file_to_someone_elses_conversation_is_forbidden(
    client, db, test_org, test_user
):
    other_user = User(
        keycloak_id=uuid.uuid4(),
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        org_id=test_org.id,
    )
    db.add(other_user)
    await db.flush()
    theirs = Conversation(
        org_id=test_org.id, user_id=other_user.id, title="Theirs", model="gpt-4o-mini"
    )
    db.add(theirs)
    await db.commit()

    response = await client.post(
        f"/v1/conversations/{theirs.id}/attachments",
        files={"file": ("notes.md", b"# hello", "text/markdown")},
    )

    assert response.status_code == 403


async def test_attach_file_to_missing_conversation_is_not_found(client):
    response = await client.post(
        f"/v1/conversations/{uuid.uuid4()}/attachments",
        files={"file": ("notes.md", b"# hello", "text/markdown")},
    )

    assert response.status_code == 404
