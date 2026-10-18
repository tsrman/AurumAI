import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.organization import Organization
from src.models.user import User
from src.routers import admin_users


def _app(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    *,
    role: str = "org_admin",
    authenticated: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_users.router, prefix="/v1/admin")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_org_db_session] = session_override
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: UserInfo(
            user_id=str(test_user.id),
            org_id=str(test_org.id),
            role=role,
        )
    return app


@pytest.mark.asyncio
async def test_admin_users_returns_scoped_rows_and_pagination(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    test_user.full_name = "Admin User"
    test_user.role = "org_admin"
    test_user.status = "active"
    test_user.last_login_at = datetime(2026, 9, 2, 9, tzinfo=timezone.utc)
    foreign_org = Organization(
        name="Foreign router org",
        slug=f"foreign-router-{uuid.uuid4().hex}",
    )
    db.add(foreign_org)
    await db.flush()
    db.add(
        User(
            keycloak_id=uuid.uuid4(),
            email="foreign-router@example.com",
            full_name="Foreign Router User",
            org_id=foreign_org.id,
            role="member",
            status="active",
        )
    )
    await db.commit()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/admin/users?limit=1&offset=0&search=Admin")

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "id": str(test_user.id),
            "email": test_user.email,
            "fullName": "Admin User",
            "role": "org_admin",
            "status": "active",
            "lastLoginAt": "2026-09-02T09:00:00Z",
            "createdAt": test_user.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert response.json()["pagination"] == {
        "cursor": None,
        "hasMore": False,
        "total": 1,
    }
    assert "foreign-router@example.com" not in response.text


@pytest.mark.asyncio
async def test_admin_users_rejects_member_and_anonymous_callers(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user, role="member")),
        base_url="http://test",
    ) as member:
        member_response = await member.get("/v1/admin/users")
    async with httpx.AsyncClient(
        transport=ASGITransport(
            app=_app(db, test_org, test_user, authenticated=False)
        ),
        base_url="http://test",
    ) as anonymous:
        anonymous_response = await anonymous.get("/v1/admin/users")

    assert member_response.status_code == 403
    assert anonymous_response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "offset=-1",
        f"search={'x' * 201}",
        f"status={'x' * 21}",
    ],
)
async def test_admin_users_validates_query_bounds(
    query: str,
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/v1/admin/users?{query}")

    assert response.status_code == 422
