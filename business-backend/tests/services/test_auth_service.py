import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.models.organization import Organization
from src.models.user import User
from src.services.auth_service import (
    LAST_LOGIN_REFRESH_INTERVAL,
    AuthService,
    auth_service,
)


class FakeResponse:
    def __init__(self, json_data: dict[str, Any], status_error: Exception | None = None) -> None:
        self._json_data = json_data
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    def json(self) -> dict[str, Any]:
        return self._json_data


class FakePostAsyncClient:
    last_instance: "FakePostAsyncClient | None" = None
    response: FakeResponse

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.post_calls: list[dict[str, Any]] = []
        FakePostAsyncClient.last_instance = self

    async def __aenter__(self) -> "FakePostAsyncClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def post(self, url: str, data: dict[str, Any]) -> FakeResponse:
        self.post_calls.append({"url": url, "data": data})
        return FakePostAsyncClient.response


@pytest.mark.asyncio
async def test_exchange_code_posts_authorization_code_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakePostAsyncClient)
    FakePostAsyncClient.response = FakeResponse({"access_token": "at-1"})

    result = await AuthService().exchange_code("code-1", "verifier-1", "http://x/cb")

    assert result == {"access_token": "at-1"}
    call = FakePostAsyncClient.last_instance.post_calls[0]
    assert call["data"]["grant_type"] == "authorization_code"
    assert call["data"]["code"] == "code-1"
    assert call["data"]["code_verifier"] == "verifier-1"
    assert call["data"]["redirect_uri"] == "http://x/cb"


@pytest.mark.asyncio
async def test_exchange_code_raises_on_keycloak_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakePostAsyncClient)
    FakePostAsyncClient.response = FakeResponse(
        {}, status_error=httpx.HTTPStatusError("invalid_grant", request=None, response=None)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await AuthService().exchange_code("bad-code", "verifier-1", "http://x/cb")


@pytest.mark.asyncio
async def test_refresh_tokens_posts_refresh_token_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("httpx.AsyncClient", FakePostAsyncClient)
    FakePostAsyncClient.response = FakeResponse({"access_token": "new-at"})

    result = await AuthService().refresh_tokens("rt-1")

    assert result == {"access_token": "new-at"}
    call = FakePostAsyncClient.last_instance.post_calls[0]
    assert call["data"]["grant_type"] == "refresh_token"
    assert call["data"]["refresh_token"] == "rt-1"


@pytest.mark.asyncio
async def test_get_or_create_user_creates_new_org_and_user(db: AsyncSession) -> None:
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user_info = UserInfo(
        user_id=str(user_id),
        email="new@example.com",
        full_name="New User",
        org_id=str(org_id),
        org_name="New Org",
        role="member",
    )

    user = await AuthService()._get_or_create_user(db, user_info)
    await db.commit()

    assert str(user.id) == str(user_id)
    assert user.email == "new@example.com"

    org_result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = org_result.scalar_one()
    assert org.name == "New Org"


@pytest.mark.asyncio
async def test_get_or_create_user_updates_existing_user_full_name_and_role(
    db: AsyncSession,
) -> None:
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    org = Organization(id=org_id, name="Existing Org", slug=f"org-{org_id.hex[:8]}")
    db.add(org)
    await db.flush()
    existing = User(
        id=user_id,
        keycloak_id=user_id,
        email="existing@example.com",
        full_name="Old Name",
        org_id=org_id,
        role="member",
    )
    db.add(existing)
    await db.commit()

    user_info = UserInfo(
        user_id=str(user_id),
        email="existing@example.com",
        full_name="New Name",
        org_id=str(org_id),
        org_name="Existing Org",
        role="admin",
    )

    user = await AuthService()._get_or_create_user(db, user_info)
    await db.commit()

    assert user.id == user_id
    assert user.full_name == "New Name"
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_sync_keycloak_user_without_groups_skips_group_sync(db: AsyncSession) -> None:
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    org = Organization(id=org_id, name="Test Org", slug=f"test-{org_id.hex[:8]}")
    db.add(org)
    await db.flush()

    user_info = UserInfo(
        user_id=str(user_id),
        email="test@example.com",
        full_name="Test User",
        org_id=str(org_id),
        org_name="Test Org",
        role="member",
        raw_claims={},
    )

    synced_user = await auth_service.sync_keycloak_user(db, user_info)

    assert synced_user.id == user_id


@pytest.mark.asyncio
async def test_sync_keycloak_user_records_last_login_and_then_throttles(
    db: AsyncSession,
) -> None:
    """`last_login_at` had no writer, so it was NULL for every user forever.

    The sync runs on every JWT-authenticated request, so it is also the write
    that must not happen every time — a fresh timestamp is only taken once
    per LAST_LOGIN_REFRESH_INTERVAL.
    """
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db.add(Organization(id=org_id, name="Login Org", slug=f"login-{org_id.hex[:8]}"))
    await db.flush()
    user_info = UserInfo(
        user_id=str(user_id),
        email="last-login@example.com",
        full_name="Last Login",
        org_id=str(org_id),
        org_name="Login Org",
        role="member",
        raw_claims={},
    )

    first = await auth_service.sync_keycloak_user(db, user_info)
    recorded = first.last_login_at
    assert recorded is not None
    # Read the column back rather than trusting the in-session attribute:
    # sync commits, so the row itself must carry the timestamp.
    assert (
        await db.scalar(select(User.last_login_at).where(User.id == user_id))
    ) == recorded

    second = await auth_service.sync_keycloak_user(db, user_info)
    assert second.last_login_at == recorded

    second.last_login_at = recorded - LAST_LOGIN_REFRESH_INTERVAL
    await db.flush()
    third = await auth_service.sync_keycloak_user(db, user_info)

    assert third.last_login_at is not None
    assert third.last_login_at > recorded - LAST_LOGIN_REFRESH_INTERVAL
