"""Real discovery schedules: recent arrivals cannot consume retention batches."""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.maintenance_worker import dispatch_data_batch


@pytest_asyncio.fixture(scope="module")
async def discovery_database():
    # Discovery is deliberately global. Isolate its arrival schedule from audit
    # records and blocked candidates left by unrelated suite fixtures.
    url = make_url(settings.DATABASE_URL)
    database = "archive_discovery_" + uuid4().hex
    admin = await asyncpg.connect(
        url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    )
    await admin.execute(f"CREATE DATABASE {database}")
    target = url.set(database=database).render_as_string(hide_password=False)
    try:
        conn = await asyncpg.connect(
            url.set(drivername="postgresql", database=database).render_as_string(
                hide_password=False
            )
        )
        await conn.execute("CREATE SCHEMA partman; CREATE EXTENSION pg_partman SCHEMA partman")
        await conn.close()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "APP_DATABASE_URL": target},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await proc.wait() == 0, "isolated archive discovery migration failed"
        yield target
    finally:
        await admin.execute(f"DROP DATABASE {database}")
        await admin.close()


@pytest.fixture
async def discovery_fixture(discovery_database):
    engine = create_async_engine(discovery_database, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    orgs = [uuid4() for _ in range(7)]
    conversations = [uuid4() for _ in orgs]
    async with sessions.begin() as db:
        for age in (100, 200):
            for table in ("conversations", "audit_logs"):
                await db.execute(
                    text("SELECT platform_ensure_partition(:t,:at)"),
                    dict(t=table, at=now - timedelta(days=age)),
                )
        for index, (org, conversation) in enumerate(zip(orgs, conversations, strict=True)):
            user = uuid4()
            at = now - timedelta(days=100 if index < 2 else 200 if index == 2 else 1)
            await db.execute(
                text("INSERT INTO organizations(id,name,slug) VALUES(:o,'discovery',:slug)"),
                dict(o=org, slug=str(org)),
            )
            await db.execute(
                text("INSERT INTO users(id,org_id,keycloak_id,email) VALUES(:u,:o,:u,:email)"),
                dict(u=user, o=org, email=f"{user}@test.invalid"),
            )
            await db.execute(
                text(
                    "INSERT INTO conversations(id,org_id,user_id,model,created_at,updated_at) VALUES(:c,:o,:u,'fixture',:at,:at)"
                ),
                dict(c=conversation, o=org, u=user, at=at),
            )
            await db.execute(
                text(
                    "INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES(:id,:o,'GET','/fixture',200,:at)"
                ),
                dict(id=uuid4(), o=org, at=at if index != 2 else now),
            )
        # Previously checked while recent; both now pass the custom 90-day cutoff.
        await db.execute(
            text("UPDATE conversation_identities SET archive_checked_at=:at WHERE id=ANY(:ids)"),
            dict(at=now - timedelta(days=20), ids=conversations[:2]),
        )
        for org in orgs[:2]:
            await db.execute(
                text("INSERT INTO archive_audit_dispatch(org_id,checked_at) VALUES(:o,:at)"),
                dict(o=org, at=now - timedelta(days=20)),
            )
        # An old payload restored yesterday must not consume the batch either.
        await db.execute(
            text("UPDATE conversation_identities SET restored_at=:at WHERE id=:c"),
            dict(at=now - timedelta(days=1), c=conversations[2]),
        )
    try:
        yield sessions, orgs, conversations, now
    finally:
        async with sessions.begin() as db:
            await db.execute(text("DELETE FROM audit_logs WHERE org_id=ANY(:ids)"), dict(ids=orgs))
            await db.execute(text("DELETE FROM organizations WHERE id=ANY(:ids)"), dict(ids=orgs))
        await engine.dispose()


@asynccontextmanager
async def lifecycle_session(sessions):
    async with sessions() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_lifecycle"))
        yield db


class Dispatches:
    def __init__(self):
        self.requests = []

    async def start_workflow(self, _run, request, **_kwargs):
        self.requests.append(request)


async def test_recent_arrivals_cannot_starve_checked_eligible_candidates(
    discovery_fixture, monkeypatch
):
    sessions, orgs, conversations, _now = discovery_fixture
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_SIZE", 1)
    monkeypatch.setattr(settings, "ARCHIVE_RETENTION_DAYS", 90)
    client = Dispatches()
    # Eligible but blocked candidates remain active. Two sweeps must fairly visit
    # both despite more than a batch of new, unchecked recent identities/orgs.
    for _ in range(2):
        await dispatch_data_batch(
            client, sessions=lambda: lifecycle_session(sessions), new_claims=True
        )
    assert {r.conversation_id for r in client.requests if not r.audit} == set(
        map(str, conversations[:2])
    )
    assert {r.org_id for r in client.requests if r.audit} == set(map(str, orgs[:2]))
    client.requests.clear()
    monkeypatch.setattr(settings, "ARCHIVE_RETENTION_DAYS", 180)
    await dispatch_data_batch(client, sessions=lambda: lifecycle_session(sessions), new_claims=True)
    assert client.requests == []


async def test_discovery_does_not_wait_for_payload_with_registry_locked(
    discovery_fixture, monkeypatch
):
    sessions, _orgs, conversations, _now = discovery_fixture
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_SIZE", 1)
    monkeypatch.setattr(settings, "ARCHIVE_RETENTION_DAYS", 90)
    async with sessions() as writer:
        await writer.execute(
            text("SELECT id FROM conversations WHERE id=:c FOR UPDATE"), dict(c=conversations[0])
        )
        try:
            # Discovery only reads payload with MVCC; it must finish while a
            # normal writer owns the payload and may next need its identity.
            await asyncio.wait_for(
                dispatch_data_batch(
                    Dispatches(), sessions=lambda: lifecycle_session(sessions), new_claims=True
                ),
                3,
            )
            await asyncio.wait_for(
                writer.execute(
                    text("SELECT id FROM conversation_identities WHERE id=:c FOR UPDATE"),
                    dict(c=conversations[0]),
                ),
                3,
            )
        finally:
            await writer.rollback()


async def test_discovery_function_privileges(discovery_fixture):
    sessions, _orgs, _conversations, _now = discovery_fixture
    async with sessions() as db:
        for name in ("platform_archive_discover", "platform_archive_audit_orgs"):
            signature = f"{name}(integer,timestamptz)"
            for role, allowed in (("aurum_ai_app", False), ("aurum_ai_lifecycle", True)):
                assert (
                    await db.scalar(
                        text("SELECT has_function_privilege(:role,:fn,'EXECUTE')"),
                        dict(role=role, fn=signature),
                    )
                    is allowed
                )


@pytest.mark.parametrize("field", ["updated_at", "last_message_at", "restored_at"])
async def test_discovery_uses_same_strict_effective_age(discovery_fixture, field):
    sessions, _orgs, conversations, now = discovery_fixture
    cutoff = now - timedelta(days=90)
    conversation = conversations[0]
    async with sessions.begin() as db:
        table = "conversation_identities" if field == "restored_at" else "conversations"
        await db.execute(
            text(f"UPDATE {table} SET {field}=:cutoff WHERE id=:c"),
            dict(c=conversation, cutoff=cutoff),
        )
    async with lifecycle_session(sessions) as db:
        rows = (
            (
                await db.execute(
                    text("SELECT conversation_id FROM platform_archive_discover(100,:cutoff)"),
                    dict(cutoff=cutoff),
                )
            )
            .scalars()
            .all()
        )
        assert conversation not in rows
        assert conversations[1] in rows


async def test_blocked_candidates_revisited_during_sustained_eligible_arrivals(
    discovery_fixture, monkeypatch
):
    from collections import Counter

    sessions, orgs, conversations, _now = discovery_fixture
    monkeypatch.setattr(settings, "ARCHIVE_BATCH_SIZE", 1)
    monkeypatch.setattr(settings, "ARCHIVE_RETENTION_DAYS", 90)
    original_orgs = orgs[:2]
    original_conversations = conversations[:2]
    client = Dispatches()
    # Two newly eligible arrivals per sweep exceed the batch capacity. Checked
    # candidates remain blocked/active. New arrivals must enter behind earlier
    # checks, so the finite queue ahead of a blocked candidate can still drain.
    for _ in range(8):
        async with sessions.begin() as db:
            at = datetime.now(timezone.utc) - timedelta(days=90, microseconds=1)
            for table in ("conversations", "audit_logs"):
                await db.execute(
                    text("SELECT platform_ensure_partition(:t,:at)"), dict(t=table, at=at)
                )
            for _ in range(2):
                org, user, conversation = uuid4(), uuid4(), uuid4()
                orgs.append(org)
                conversations.append(conversation)
                await db.execute(
                    text("INSERT INTO organizations(id,name,slug) VALUES(:o,'arrival',:slug)"),
                    dict(o=org, slug=str(org)),
                )
                await db.execute(
                    text("INSERT INTO users(id,org_id,keycloak_id,email) VALUES(:u,:o,:u,:email)"),
                    dict(u=user, o=org, email=f"{user}@test.invalid"),
                )
                await db.execute(
                    text(
                        "INSERT INTO conversations(id,org_id,user_id,model,created_at,updated_at) VALUES(:c,:o,:u,'fixture',:at,:at)"
                    ),
                    dict(c=conversation, o=org, u=user, at=at),
                )
                await db.execute(
                    text(
                        "INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES(:id,:o,'GET','/arrival',200,:at)"
                    ),
                    dict(id=uuid4(), o=org, at=at),
                )
        await dispatch_data_batch(
            client, sessions=lambda: lifecycle_session(sessions), new_claims=True
        )
    conversations_checked = Counter(r.conversation_id for r in client.requests if not r.audit)
    orgs_checked = Counter(r.org_id for r in client.requests if r.audit)
    assert all(conversations_checked[str(value)] >= 2 for value in original_conversations)
    assert all(orgs_checked[str(value)] >= 2 for value in original_orgs)
