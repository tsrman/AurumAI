import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.dependencies import get_org_db_session


# This test builds its own engine instead of going through Settings, so it
# needs the URL from the environment directly. APP_DATABASE_URL is the variable
# every other part of the stack uses (env_prefix="APP_"); accepting it here too
# means a shell set up for the app doesn't silently send this one test to
# localhost and fail with a connection error that looks like a code bug.
DATABASE_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("APP_DATABASE_URL")
    or "postgresql+asyncpg://postgres:postgres@localhost:5432/aurum_ai"
)


class FakeUser:
    def __init__(self):
        self.org_id = "11111111-1111-1111-1111-111111111111"
        self.raw_claims = {}


@pytest.mark.asyncio
async def test_org_session_cleanup_does_not_commit_on_error(monkeypatch):
    test_engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    test_session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("src.core.dependencies.async_session", test_session)

    async def set_test_org(user, session):
        session.info["org_id"] = user.org_id

    monkeypatch.setattr("src.core.dependencies._set_org_and_sync", set_test_org)

    try:
        slug = f"rollback-test-{uuid.uuid4()}"
        gen = get_org_db_session(FakeUser())
        session: AsyncSession = await gen.__anext__()
        await session.execute(
            text("INSERT INTO organizations (id, name, slug) " "VALUES (:id, :name, :slug)"),
            {"id": str(uuid.uuid4()), "name": "Rollback Test", "slug": slug},
        )
        try:
            await gen.athrow(RuntimeError("boom"))
        except RuntimeError:
            pass
        finally:
            await gen.aclose()

        async with test_session() as check_session:
            result = await check_session.execute(
                text("SELECT id FROM organizations WHERE slug = :slug"),
                {"slug": slug},
            )
            rows = result.all()

            org_result = await check_session.execute(
                text("SELECT current_setting('app.current_org_id', true)")
            )
            current_org = org_result.scalar()

        assert len(rows) == 0, "organization inserted during failed session was persisted"
        assert current_org is None or current_org == "", "org context was not cleared"
    finally:
        await test_engine.dispose()
