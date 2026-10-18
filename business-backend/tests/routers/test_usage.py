import uuid
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.middleware.error_handler import setup_exception_handlers
from src.routers import usage
from src.services.quota import UsageSnapshot, get_quota_service

ORG_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"


class SnapshotQuotaService:
    def __init__(self, snapshot: UsageSnapshot) -> None:
        self._snapshot = snapshot
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def snapshot(
        self, org_id: uuid.UUID, user_id: uuid.UUID, **_: object
    ) -> UsageSnapshot:
        self.calls.append((org_id, user_id))
        return self._snapshot


def _user() -> UserInfo:
    return UserInfo(user_id=USER_ID, org_id=ORG_ID, role="member")


def _app(
    quota_service: SnapshotQuotaService,
    *,
    authenticated: bool = True,
    provisioned: list[bool] | None = None,
) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(usage.router, prefix="/v1")
    app.dependency_overrides[get_quota_service] = lambda: quota_service
    # The route depends on get_org_db_session purely for its provisioning side
    # effect (sync_keycloak_user + commit); stubbed here so these stay unit
    # tests, and recorded so one of them can assert it really runs.
    def _org_session() -> None:
        if provisioned is not None:
            provisioned.append(True)
        return None

    app.dependency_overrides[get_org_db_session] = _org_session
    if authenticated:
        app.dependency_overrides[get_current_user] = _user
    return app


@pytest.mark.asyncio
async def test_usage_me_provisions_the_org_before_reading_the_snapshot() -> None:
    """A dashboard can call /v1/usage/me as the very first request after
    login. Without the org-scoped session dependency nothing runs
    sync_keycloak_user, quota.snapshot finds no organization row, and the
    caller gets a 500 from QuotaInvariantError instead of a zeroed summary."""
    quota = SnapshotQuotaService(
        UsageSnapshot(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
            org_requests_used=0,
            org_tokens_used=0,
            user_requests_used=0,
            user_tokens_used=0,
            request_limit=None,
            token_limit=None,
        )
    )
    provisioned: list[bool] = []
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(quota, provisioned=provisioned)),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/usage/me")

    assert response.status_code == 200
    assert provisioned == [True]


@pytest.mark.asyncio
async def test_usage_me_requires_authentication() -> None:
    quota = SnapshotQuotaService(
        UsageSnapshot(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
            org_requests_used=0,
            org_tokens_used=0,
            user_requests_used=0,
            user_tokens_used=0,
            request_limit=None,
            token_limit=None,
        )
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(quota, authenticated=False)),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/usage/me")

    assert response.status_code == 401
    assert quota.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_limit", "token_limit", "requests_remaining", "tokens_remaining"),
    [(100, 8_000, 60, 0), (None, None, None, None)],
)
async def test_usage_me_returns_personal_and_organization_monthly_totals(
    request_limit: int | None,
    token_limit: int | None,
    requests_remaining: int | None,
    tokens_remaining: int | None,
) -> None:
    quota = SnapshotQuotaService(
        UsageSnapshot(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
            org_requests_used=40,
            org_tokens_used=9_000,
            user_requests_used=12,
            user_tokens_used=3_456,
            request_limit=request_limit,
            token_limit=token_limit,
        )
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(quota)), base_url="http://test"
    ) as client:
        response = await client.get("/v1/usage/me")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "periodStart": "2026-08-01",
        "periodEnd": "2026-09-01",
        "user": {"requestsUsed": 12, "tokensUsed": 3_456},
        "organization": {
            "requestsUsed": 40,
            "tokensUsed": 9_000,
            "requestLimit": request_limit,
            "tokenLimit": token_limit,
            "requestsRemaining": requests_remaining,
            "tokensRemaining": tokens_remaining,
        },
    }
    assert response.json()["meta"]["requestId"].startswith("req_")
    assert quota.calls == [(uuid.UUID(ORG_ID), uuid.UUID(USER_ID))]
