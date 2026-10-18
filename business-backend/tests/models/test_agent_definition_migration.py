import os
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings


def _runtime_db_url() -> str:
    parsed = urlparse(settings.DATABASE_URL)
    password = os.environ.get("APP_DATABASE_ROLE_PASSWORD", "aurum_ai_app")
    return urlunparse(
        parsed._replace(
            netloc=f"aurum_ai_app:{password}@{parsed.hostname}:{parsed.port or 5432}"
        )
    )


async def test_agent_definition_migration_enforces_rls_and_runtime_dml(
    db: AsyncSession,
) -> None:
    table = (
        await db.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = 'agent_definitions'"
            )
        )
    ).one()
    policy = (
        await db.execute(
            text(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE tablename = 'agent_definitions' "
                "AND policyname = 'agent_definitions_org_isolation'"
            )
        )
    ).one()
    privileges = (
        await db.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'aurum_ai_app' "
                "AND table_name = 'agent_definitions'"
            )
        )
    ).scalars().all()

    assert table == (True, True)
    assert "app.current_org_id" in policy.qual
    assert "app.current_org_id" in policy.with_check
    assert set(privileges) == {"SELECT", "INSERT", "UPDATE", "DELETE"}


async def test_agent_definitions_rls_hides_and_rejects_cross_org_rows() -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    forged = uuid.uuid4()
    admin_engine = create_async_engine(settings.DATABASE_URL)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    app_engine = create_async_engine(_runtime_db_url())
    app_sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    try:
        async with admin_sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) "
                    "VALUES (:id, :name, :slug)"
                ),
                [
                    {"id": org_a, "name": "Agent RLS A", "slug": f"agent-a-{org_a}"},
                    {"id": org_b, "name": "Agent RLS B", "slug": f"agent-b-{org_b}"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO agent_definitions "
                    "(id, org_id, name, model_id, system_prompt) "
                    "VALUES (:id, :org_id, :name, 'model', 'prompt')"
                ),
                [
                    {"id": agent_a, "org_id": org_a, "name": "A private"},
                    {"id": agent_b, "org_id": org_b, "name": "B private"},
                ],
            )
            await session.commit()

        async with app_sessions() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            visible = (
                await session.execute(text("SELECT id FROM agent_definitions"))
            ).scalars().all()
            assert visible == [agent_a]

        with pytest.raises(ProgrammingError):
            async with app_sessions() as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO agent_definitions "
                        "(id, org_id, name, model_id, system_prompt) "
                        "VALUES (:id, :org_id, 'forged', 'model', 'prompt')"
                    ),
                    {"id": forged, "org_id": org_b},
                )
                await session.commit()
    finally:
        await app_engine.dispose()
        async with admin_sessions() as session:
            await session.execute(
                text("DELETE FROM agent_definitions WHERE id IN (:a, :b, :forged)"),
                {"a": agent_a, "b": agent_b, "forged": forged},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.commit()
        await admin_engine.dispose()
