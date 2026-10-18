import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user
from src.core.capability_catalog import Capability
from src.core.dependencies import get_db, get_org_db_session
from src.routers import auth as auth_router


@pytest_asyncio.fixture
async def client(db):
    async def override_get_db():
        yield db

    test_app = FastAPI()
    test_app.include_router(auth_router.router, prefix="/v1/auth")
    test_app.dependency_overrides[get_db] = override_get_db

    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_callback_exchanges_code_and_sets_auth_cookies(client):
    tokens = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
    payload = {"sub": "user-1", "email": "a@example.com", "role": "member"}

    with patch(
        "src.routers.auth.auth_service.exchange_code", new_callable=AsyncMock
    ) as mock_exchange, patch(
        "src.core.keycloak.keycloak_client.decode_token",
        new_callable=AsyncMock,
        return_value=payload,
    ), patch(
        "src.routers.auth.auth_service.sync_keycloak_user", new_callable=AsyncMock
    ):
        mock_exchange.return_value = tokens
        response = await client.post(
            "/v1/auth/callback",
            json={"code": "abc", "code_verifier": "verifier", "redirect_uri": "http://x/cb"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "authenticated"
    mock_exchange.assert_awaited_once_with("abc", "verifier", "http://x/cb")
    assert response.cookies["access_token"] == "at-1"
    assert response.cookies["refresh_token"] == "rt-1"


@pytest.mark.asyncio
async def test_callback_without_refresh_token_omits_refresh_cookie(client):
    tokens = {"access_token": "at-1", "expires_in": 3600}
    payload = {"sub": "user-1"}

    with patch(
        "src.routers.auth.auth_service.exchange_code", new_callable=AsyncMock
    ) as mock_exchange, patch(
        "src.core.keycloak.keycloak_client.decode_token",
        new_callable=AsyncMock,
        return_value=payload,
    ), patch(
        "src.routers.auth.auth_service.sync_keycloak_user", new_callable=AsyncMock
    ):
        mock_exchange.return_value = tokens
        response = await client.post(
            "/v1/auth/callback",
            json={"code": "abc", "code_verifier": "verifier", "redirect_uri": "http://x/cb"},
        )

    assert response.status_code == 200
    assert "refresh_token" not in response.cookies


@pytest.mark.asyncio
async def test_callback_propagates_keycloak_exchange_failure(client):
    with patch(
        "src.routers.auth.auth_service.exchange_code", new_callable=AsyncMock
    ) as mock_exchange:
        mock_exchange.side_effect = httpx.HTTPStatusError(
            "invalid_grant", request=None, response=None
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.post(
                "/v1/auth/callback",
                json={
                    "code": "bad-code",
                    "code_verifier": "verifier",
                    "redirect_uri": "http://x/cb",
                },
            )


@pytest.mark.asyncio
async def test_refresh_sets_new_access_token_cookie(client):
    with patch(
        "src.routers.auth.auth_service.refresh_tokens", new_callable=AsyncMock
    ) as mock_refresh:
        mock_refresh.return_value = {"access_token": "new-at", "expires_in": 3600}
        response = await client.post("/v1/auth/refresh", json={"refresh_token": "rt-1"})

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "refreshed"
    assert response.cookies["access_token"] == "new-at"
    mock_refresh.assert_awaited_once_with("rt-1")


@pytest.mark.asyncio
async def test_logout_clears_cookies(client):
    response = await client.post("/v1/auth/logout")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "logged_out"
    set_cookie_headers = response.headers.get_list("set-cookie")
    assert any("access_token=" in h and "Max-Age=0" in h for h in set_cookie_headers)
    assert any("refresh_token=" in h and "Max-Age=0" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_me_returns_current_user_without_auth_returns_401(client):
    response = await client.get("/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_current_user_with_sorted_live_capabilities(db):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    def override_get_current_user():
        return UserInfo(
            user_id=user_id,
            email="a@example.com",
            full_name="A",
            org_id=org_id,
            org_name="Org",
            role="org_admin",
        )

    async def override_get_org_db_session():
        yield db

    test_app = FastAPI()
    test_app.include_router(auth_router.router, prefix="/v1/auth")
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session

    with patch.object(
        auth_router.authorization_service,
        "effective_capabilities",
        AsyncMock(
            return_value={
                Capability.WORKSPACE_KNOWLEDGE_CREATE,
                Capability.CHAT_SEND,
            }
        ),
    ) as effective_capabilities:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/auth/me")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["user_id"] == user_id
    assert data["role"] == "org_admin"
    assert data["capabilities"] == ["chat.send", "workspace.knowledge.create"]
    assert "permissions" not in data
    subject = effective_capabilities.await_args.args[1]
    assert str(subject.user_id) == user_id
    assert str(subject.org_id) == org_id


@pytest.mark.asyncio
async def test_me_resolves_capabilities_on_every_request(db):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    def override_get_current_user():
        return UserInfo(
            user_id=user_id,
            email="member@example.com",
            full_name="Member",
            org_id=org_id,
            org_name="Org",
            role="member",
        )

    async def override_get_org_db_session():
        yield db

    test_app = FastAPI()
    test_app.include_router(auth_router.router, prefix="/v1/auth")
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[get_org_db_session] = override_get_org_db_session

    effective_capabilities = AsyncMock(
        side_effect=[
            {Capability.CHAT_SEND, Capability.WORKSPACE_KNOWLEDGE_CREATE},
            {Capability.CHAT_SEND},
        ]
    )
    with patch.object(
        auth_router.authorization_service,
        "effective_capabilities",
        effective_capabilities,
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            first = await client.get("/v1/auth/me")
            second = await client.get("/v1/auth/me")

    assert first.json()["data"]["capabilities"] == [
        "chat.send",
        "workspace.knowledge.create",
    ]
    assert second.json()["data"]["capabilities"] == ["chat.send"]
    assert effective_capabilities.await_count == 2
