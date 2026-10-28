"""Hermes Agent's per-user memory: org-isolated, per user, versioned, bounded."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.user import User
from src.services import hermes_memory
from src.services.hermes_memory import ENTRY_DELIMITER, MemoryConflict, bounded


async def test_table_forces_row_security_with_an_org_policy(db) -> None:
    row = (
        await db.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'hermes_memories'"))
    ).one()
    policies = (
        await db.execute(text("SELECT policyname FROM pg_policies WHERE tablename = 'hermes_memories'"))
    ).scalars().all()
    grants = set(
        (
            await db.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = 'hermes_memories' AND grantee = 'aurum_ai_app'"
                )
            )
        ).scalars()
    )

    assert row == (True, True)
    assert policies == ["hermes_memories_org_isolation"]
    assert grants == {"SELECT", "INSERT", "UPDATE", "DELETE"}


async def test_writes_need_the_version_they_read_and_clearing_refuses_older_reads(db, test_org, test_user) -> None:
    org, user = test_org.id, test_user.id

    assert await hermes_memory.read(db, org, user) == {
        "memory": hermes_memory.Memory("", 0),
        "user": hermes_memory.Memory("", 0),
    }
    assert await hermes_memory.write(db, org, user, "memory", "first", 0) == 1
    with pytest.raises(MemoryConflict):
        await hermes_memory.write(db, org, user, "memory", "a second insert", 0)
    assert await hermes_memory.write(db, org, user, "memory", "second", 1) == 2
    with pytest.raises(MemoryConflict):
        await hermes_memory.write(db, org, user, "memory", "stale", 1)

    await hermes_memory.clear(db, org, user)

    with pytest.raises(MemoryConflict):
        await hermes_memory.write(db, org, user, "memory", "read before the clear", 2)
    assert (await hermes_memory.read(db, org, user))["memory"] == hermes_memory.Memory("", 3)


async def test_another_user_of_the_organization_neither_sees_nor_clears_it(db, test_org, test_user) -> None:
    other = User(keycloak_id=uuid.uuid4(), email=f"other-{uuid.uuid4().hex[:8]}@example.com", org_id=test_org.id)
    db.add(other)
    await db.flush()
    await hermes_memory.write(db, test_org.id, test_user.id, "user", "likes tea", 0)

    assert (await hermes_memory.read(db, test_org.id, other.id))["user"].content == ""
    await hermes_memory.clear(db, test_org.id, other.id)
    assert (await hermes_memory.read(db, test_org.id, test_user.id))["user"].content == "likes tea"


def test_what_the_sandbox_wrote_is_cut_at_an_entry_within_the_limit() -> None:
    entries = [f"fact {index} " + "x" * 300 for index in range(10)]
    stored = bounded("memory", ENTRY_DELIMITER.join(entries) + "\x00")

    assert len(stored) <= 2200 and stored.endswith("x" * 300)
    assert stored.split(ENTRY_DELIMITER) == entries[: len(stored.split(ENTRY_DELIMITER))]
    assert bounded("user", "y" * 5000) == "y" * 1375


async def test_the_database_bounds_the_content_too(db, test_org, test_user) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text("INSERT INTO hermes_memories(org_id,user_id,kind,content) VALUES(:o,:u,'user',:c)"),
                {"o": test_org.id, "u": test_user.id, "c": "z" * 1376},
            )


async def test_users_read_and_clear_only_their_own_memory(db, test_org, test_user) -> None:
    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from src.core.auth import UserInfo, get_current_user
    from src.core.dependencies import get_org_db_session
    from src.routers import hermes_memory as route

    await hermes_memory.write(db, test_org.id, test_user.id, "memory", "remembers the repo", 0)
    app = FastAPI()
    app.include_router(route.router, prefix="/v1")

    async def session():  # noqa: ANN202
        yield db

    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(test_user.id), org_id=str(test_org.id), role="member"
    )
    app.dependency_overrides[get_org_db_session] = session
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        shown = (await client.get("/v1/hermes/memory")).json()["data"]
        cleared = await client.delete("/v1/hermes/memory")
        after = (await client.get("/v1/hermes/memory")).json()["data"]

    assert shown == {"memory": "remembers the repo", "user": ""}
    assert cleared.status_code == 204 and after == {"memory": "", "user": ""}
