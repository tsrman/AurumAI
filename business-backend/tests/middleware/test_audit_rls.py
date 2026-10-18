"""Verify real audit_logs insertion and tenant RLS.

Unlike test_audit.py, the middleware writes to PostgreSQL. Reads use
aurum_ai_app, with FORCE ROW LEVEL SECURITY enforced on the table.
"""

import os
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse, urlunparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import engine as shared_engine
from src.middleware.audit import AuditMiddleware

pytestmark = pytest.mark.asyncio


def _runtime_db_url() -> str:
    """Connect as the application role, not a superuser."""
    parsed = urlparse(settings.DATABASE_URL)
    password = os.environ.get("APP_DATABASE_ROLE_PASSWORD", "aurum_ai_app")
    return urlunparse(
        parsed._replace(
            netloc=f"aurum_ai_app:{password}@{parsed.hostname}:{parsed.port or 5432}"
        )
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)

    @app.post("/v1/skills/{skill_id}")
    async def touch_skill(skill_id: str) -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _insert_org(session_factory, org_id: uuid.UUID) -> None:
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": org_id, "name": "Audit Test", "slug": f"audit-{org_id}"},
        )
        await session.commit()


async def test_record_lands_in_the_database_and_is_org_scoped():
    admin_engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    user_id = uuid.uuid4()
    await _insert_org(admin_sessions, org_a)
    await _insert_org(admin_sessions, org_b)

    skill_id = uuid.uuid4()
    with patch(
        "src.middleware.audit._rate_limit_identity",
        AsyncMock(return_value=(str(org_a), str(user_id))),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/v1/skills/{skill_id}", headers={"user-agent": "pytest-audit"}
            )
    assert response.status_code == 200

    role_engine = create_async_engine(_runtime_db_url(), poolclass=NullPool)
    app_sessions = async_sessionmaker(role_engine, expire_on_commit=False)
    try:
        # The owning organization can read its audit record...
        async with app_sessions() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(org_a)},
            )
            rows = (
                await session.execute(
                    text(
                        "SELECT method, path, status_code, user_agent, user_id "
                        "FROM audit_logs WHERE org_id = :org"
                    ),
                    {"org": org_a},
                )
            ).all()

        assert len(rows) == 1
        method, path, status_code, user_agent, logged_user = rows[0]
        assert method == "POST"
        # Store a route template, not a particular skill identifier.
        assert path == "/v1/skills/{skill_id}"
        assert str(skill_id) not in path
        assert status_code == 200
        assert user_agent == "pytest-audit"
        assert logged_user == user_id

        # ...but another organization cannot, even when its org_id is known.
        async with app_sessions() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(org_b)},
            )
            leaked = (
                await session.execute(
                    text("SELECT count(*) FROM audit_logs WHERE org_id = :org"),
                    {"org": org_a},
                )
            ).scalar()
        assert leaked == 0
    finally:
        # Middleware uses shared async_session; its pool may keep a connection
        # opened in this test event loop. pytest-asyncio gives each test a new
        # loop, so a pooled connection would break the next test that borrows it
        # (do_ping cannot operate on another event loop).
        await shared_engine.dispose()
        async with admin_sessions() as session:
            await session.execute(
                text("DELETE FROM audit_logs WHERE org_id = ANY(:orgs)"),
                {"orgs": [org_a, org_b]},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = ANY(:orgs)"),
                {"orgs": [org_a, org_b]},
            )
            await session.commit()
        await role_engine.dispose()
        await admin_engine.dispose()
