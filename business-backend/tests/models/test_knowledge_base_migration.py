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


async def test_knowledge_bases_migration_enforces_rls_and_runtime_dml(
    db: AsyncSession,
) -> None:
    table = (
        await db.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = 'knowledge_bases'"
            )
        )
    ).one()
    policy = (
        await db.execute(
            text(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE tablename = 'knowledge_bases' "
                "AND policyname = 'knowledge_bases_org_isolation'"
            )
        )
    ).one()
    privileges = (
        await db.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'aurum_ai_app' AND table_name = 'knowledge_bases'"
            )
        )
    ).scalars().all()

    assert table == (True, True)
    assert "app.current_org_id" in policy.qual
    assert "app.current_org_id" in policy.with_check
    assert set(privileges) == {"SELECT", "INSERT", "UPDATE", "DELETE"}


async def test_knowledge_bases_rls_blocks_cross_org_reads_and_writes() -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    base_a = uuid.uuid4()
    base_b = uuid.uuid4()
    forged_base = uuid.uuid4()
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
                    {"id": org_a, "name": "KB RLS A", "slug": f"kb-rls-a-{org_a}"},
                    {"id": org_b, "name": "KB RLS B", "slug": f"kb-rls-b-{org_b}"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO knowledge_bases (id, org_id, name) "
                    "VALUES (:id, :org_id, :name)"
                ),
                [
                    {"id": base_a, "org_id": org_a, "name": "A private"},
                    {"id": base_b, "org_id": org_b, "name": "B private"},
                ],
            )
            await session.commit()

        async with app_sessions() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            visible = (
                await session.execute(text("SELECT id FROM knowledge_bases"))
            ).scalars().all()
            assert visible == [base_a]

            updated = await session.execute(
                text("UPDATE knowledge_bases SET name = 'forged' WHERE id = :id"),
                {"id": base_b},
            )
            assert updated.rowcount == 0
            await session.commit()

        with pytest.raises(ProgrammingError):
            async with app_sessions() as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO knowledge_bases (id, org_id, name) "
                        "VALUES (:id, :org_id, 'cross-org')"
                    ),
                    {"id": forged_base, "org_id": org_b},
                )
                await session.commit()
    finally:
        await app_engine.dispose()
        async with admin_sessions() as session:
            await session.execute(
                text("DELETE FROM knowledge_bases WHERE id IN (:a, :b, :forged)"),
                {"a": base_a, "b": base_b, "forged": forged_base},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.commit()
        await admin_engine.dispose()
