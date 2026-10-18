from contextlib import asynccontextmanager
from typing import AsyncIterator
import uuid

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
import jwt
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from src.core import auth, dependencies
from src.core.auth import UserInfo, get_current_user_or_session
from src.core.config import settings
from src.core.dependencies import get_service_db_session


def _request(*, conversation_id: str | None = None) -> Request:
    path_params = {"conversation_id": conversation_id} if conversation_id else {}
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/conversations/{conversation_id}" if conversation_id else "/internal",
            "headers": [],
            "query_string": b"",
            "path_params": path_params,
        }
    )


def _credentials(payload: dict[str, object]) -> HTTPAuthorizationCredentials:
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def _reject_keycloak_token(*_args: object, **_kwargs: object) -> UserInfo:
    raise HTTPException(status_code=401, detail="Invalid token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "user-1", "org_id": "org-1", "type": "session"},
        {
            "sub": "user-1",
            "org_id": "org-1",
            "conversation_id": "conv-1",
            "type": "access",
        },
    ],
)
async def test_session_auth_rejects_unbound_or_wrong_type_token(monkeypatch, payload) -> None:
    monkeypatch.setattr(auth, "get_current_user", _reject_keycloak_token)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_session(_request(), _credentials(payload))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_session_auth_rejects_legacy_permission_snapshot(
    monkeypatch,
) -> None:
    monkeypatch.setattr(auth, "get_current_user", _reject_keycloak_token)
    credentials = _credentials(
        {
            "sub": "user-1",
            "org_id": "org-1",
            "conversation_id": "conv-1",
            "type": "session",
            "permissions": ["files:read"],
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_session(_request(conversation_id="conv-1"), credentials)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_session_auth_enforces_conversation_binding(monkeypatch) -> None:
    monkeypatch.setattr(auth, "get_current_user", _reject_keycloak_token)
    credentials = _credentials(
        {
            "sub": "user-1",
            "org_id": "org-1",
            "conversation_id": "conv-1",
            "type": "session",
        }
    )

    identity = await get_current_user_or_session(
        _request(conversation_id="conv-1"), credentials
    )
    assert identity.user_id == "user-1"

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_session(_request(conversation_id="conv-2"), credentials)
    assert exc_info.value.status_code == 403


def _service_session_app(identity: UserInfo) -> FastAPI:
    app = FastAPI()

    @app.get("/identity")
    async def identity_route(
        user: UserInfo = Depends(get_current_user_or_session),
        _session: AsyncSession = Depends(get_service_db_session),
    ) -> dict[str, str]:
        return {"role": user.role}

    app.dependency_overrides[get_current_user_or_session] = lambda: identity
    return app


@pytest.mark.asyncio
async def test_service_session_hydrates_current_database_role(
    monkeypatch,
    db: AsyncSession,
    test_user,
) -> None:
    test_user.role = "org_admin"
    await db.commit()
    from src.models.conversation import Conversation
    conversation = Conversation(org_id=test_user.org_id, user_id=test_user.id, model="test")
    db.add(conversation)
    await db.commit()
    identity = UserInfo(
        user_id=str(test_user.id),
        org_id=str(test_user.org_id),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conversation.id)},
    )

    @asynccontextmanager
    async def same_session() -> AsyncIterator[AsyncSession]:
        yield db

    monkeypatch.setattr(dependencies, "async_session", same_session)
    app = _service_session_app(identity)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/identity")

    assert response.status_code == 200
    assert response.json() == {"role": "org_admin"}


@pytest.mark.asyncio
async def test_service_session_rejects_identity_missing_from_current_organization(
    monkeypatch,
    db: AsyncSession,
    test_org,
) -> None:
    identity = UserInfo(
        user_id=str(uuid.uuid4()),
        org_id=str(test_org.id),
        raw_claims={"type": "session", "conversation_id": str(uuid.uuid4())},
    )

    @asynccontextmanager
    async def same_session() -> AsyncIterator[AsyncSession]:
        yield db

    monkeypatch.setattr(dependencies, "async_session", same_session)
    app = _service_session_app(identity)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/identity")

    assert response.status_code == 401
