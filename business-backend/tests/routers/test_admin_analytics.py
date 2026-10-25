import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.organization import Organization
from src.models.user import User
from src.routers import admin_analytics


def _app(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    *,
    role: str = "org_admin",
    authenticated: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_analytics.router, prefix="/v1/admin")

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
async def test_admin_usage_returns_utc_period_limits_and_zero_series(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/admin/analytics/usage?from=2026-09-01&to=2026-09-03"
        )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "period": {"from": "2026-09-01", "to": "2026-09-03", "timezone": "UTC"},
        "limits": {"requestLimit": 10_000, "tokenLimit": 1_000_000_000},
        "quota": {
            "periodStart": (
                datetime.now(timezone.utc).date().replace(day=1).isoformat()
            ),
            "requestsUsed": 0,
            "tokensUsed": 0,
            "requestsRemaining": 10_000,
            "tokensRemaining": 1_000_000_000,
        },
        "totals": {
            "requests": 0,
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "unaccountedRequests": 0,
            "images": 0,
        },
        "daily": [
            {
                "date": value,
                "requests": 0,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "unaccountedRequests": 0,
                "images": 0,
            }
            for value in ("2026-09-01", "2026-09-02", "2026-09-03")
        ],
        "byUser": [],
        "byUserTotal": 0,
        "byModel": [],
    }


@pytest.mark.asyncio
async def test_admin_usage_defaults_to_current_utc_month(
    monkeypatch: pytest.MonkeyPatch,
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    monkeypatch.setattr(admin_analytics, "_utc_today", lambda: date(2026, 9, 17))
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/admin/analytics/usage")

    assert response.status_code == 200
    assert response.json()["data"]["period"] == {
        "from": "2026-09-01",
        "to": "2026-09-17",
        "timezone": "UTC",
    }


@pytest.mark.asyncio
async def test_admin_usage_default_from_is_current_month_when_only_to_is_given(
    monkeypatch: pytest.MonkeyPatch,
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    monkeypatch.setattr(admin_analytics, "_utc_today", lambda: date(2026, 9, 17))
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/admin/analytics/usage?to=2026-10-05")

    assert response.status_code == 200
    assert response.json()["data"]["period"] == {
        "from": "2026-09-01",
        "to": "2026-10-05",
        "timezone": "UTC",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "from=2026-09-03&to=2026-09-01",
        "from=2025-09-01&to=2026-09-02",
        "from=invalid&to=2026-09-02",
        "from=2026-09-01&to=2026-09-02&model=%20leading",
        "from=9999-12-31&to=9999-12-31",
    ],
)
async def test_admin_usage_rejects_invalid_ranges_and_models(
    query: str,
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/v1/admin/analytics/usage?{query}")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_usage_hides_foreign_user_filter(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    foreign_org = Organization(
        name="Foreign analytics filter org",
        slug=f"foreign-filter-{uuid.uuid4().hex}",
    )
    db.add(foreign_org)
    await db.flush()
    foreign_user = User(
        keycloak_id=uuid.uuid4(),
        email="foreign-filter@example.com",
        org_id=foreign_org.id,
        role="member",
        status="active",
    )
    db.add(foreign_user)
    await db.commit()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/v1/admin/analytics/usage?userId={foreign_user.id}"
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_usage_rejects_member_and_anonymous_callers(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user, role="member")),
        base_url="http://test",
    ) as member:
        member_response = await member.get("/v1/admin/analytics/usage")
    async with httpx.AsyncClient(
        transport=ASGITransport(
            app=_app(db, test_org, test_user, authenticated=False)
        ),
        base_url="http://test",
    ) as anonymous:
        anonymous_response = await anonymous.get("/v1/admin/analytics/usage")

    assert member_response.status_code == 403
    assert anonymous_response.status_code == 401


@pytest.mark.asyncio
async def test_admin_usage_serializes_unlimited_quotas_as_null(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    test_org.quota_requests = None
    test_org.quota_tokens = None
    await db.commit()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org, test_user)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/admin/analytics/usage?from=2026-09-01&to=2026-09-01"
        )

    assert response.status_code == 200
    assert response.json()["data"]["limits"] == {
        "requestLimit": None,
        "tokenLimit": None,
    }
