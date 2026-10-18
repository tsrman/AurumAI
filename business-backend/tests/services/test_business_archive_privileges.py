# ruff: noqa: F811
"""Actual SQL privileges, audit fencing, foreign UUID probe and empty-only DDL."""

from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.services.test_business_archive_service import archive_fixture, LocalArchiveStorage  # noqa:F401
from src.services.archive.service import ArchiveService


async def test_app_cannot_forge_manifest_or_call_operator_probe(archive_fixture):
    sessions, org, conversation = archive_fixture
    for sql in (
        "SELECT * FROM archive_jobs",
        "SELECT platform_archive_payload_exists('conversations',:c)",
        "SELECT * FROM platform_archive_pending(1)",
        "SELECT * FROM platform_archive_discover(1,now())",
        "SELECT * FROM platform_archive_audit_orgs(1,now())",
        "SELECT platform_drop_empty_archive_partitions(now())",
    ):
        async with sessions() as db:
            await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
            await db.execute(
                text("SELECT set_config('app.current_org_id',:o,true)"), dict(o=str(org))
            )
            with pytest.raises(DBAPIError):
                await db.execute(text(sql), dict(c=conversation))


async def test_audit_claim_fences_update_and_preserves_concurrent_insert(
    archive_fixture, tmp_path, monkeypatch
):
    from src.services.conversation_lifecycle import ArchiveBlocked

    sessions, org, _ = archive_fixture
    audit = uuid4()
    at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    async with sessions() as db:
        await db.execute(text("SELECT platform_ensure_partition('audit_logs',:at)"), dict(at=at))
        await db.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,created_at,method,path,status_code) VALUES(:id,:o,:at,'POST','/original',200)"
            ),
            dict(id=audit, o=org, at=at),
        )
        await db.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))

    async def stop(*args):
        raise ArchiveBlocked("test_after_claim")

    monkeypatch.setattr(service, "_seal", stop)
    with pytest.raises(ArchiveBlocked):
        await service.run_audit(org, apply=True)
    async with sessions() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
        await db.execute(text("SELECT set_config('app.current_org_id',:o,true)"), dict(o=str(org)))
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE audit_logs SET path='/changed' WHERE id=:id"), dict(id=audit)
            )
    async with sessions() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
        await db.execute(text("SELECT set_config('app.current_org_id',:o,true)"), dict(o=str(org)))
        await db.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,method,path,status_code) VALUES(:id,:o,'POST','/new',200)"
            ),
            dict(id=uuid4(), o=org),
        )
        await db.commit()


async def test_empty_partition_drop_never_drops_nonempty_month(archive_fixture):
    sessions, org, _ = archive_fixture
    empty = datetime(2024, 2, 1, tzinfo=timezone.utc)
    occupied = datetime(2024, 3, 1, tzinfo=timezone.utc)
    async with sessions() as db:
        for at in (empty, occupied):
            await db.execute(
                text("SELECT platform_ensure_partition('audit_logs',:at)"), dict(at=at)
            )
        await db.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,created_at,method,path,status_code) VALUES(:id,:o,:at,'POST','/retained',200)"
            ),
            dict(id=uuid4(), o=org, at=occupied),
        )
        await db.commit()
        await db.execute(text("SET LOCAL ROLE aurum_ai_lifecycle"))
        await db.execute(
            text("SELECT platform_drop_empty_archive_partitions(:cutoff)"),
            dict(cutoff=datetime(2025, 1, 1, tzinfo=timezone.utc)),
        )
        assert await db.scalar(text("SELECT to_regclass('public.audit_logs_p20240201')")) is None
        assert (
            await db.scalar(text("SELECT to_regclass('public.audit_logs_p20240301')")) is not None
        )
        await db.commit()


async def test_operator_jobs_are_tenant_scoped(archive_fixture, tmp_path):
    sessions, org, conversation = archive_fixture
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    result = await service.run(org, conversation, apply=True)
    foreign = uuid4()
    assert await service.list(foreign) == []
    from src.services.conversation_lifecycle import ArchiveBlocked

    with pytest.raises(ArchiveBlocked, match="archive_not_found"):
        await service.restore(foreign, result["archive_id"], apply=True)


async def test_pending_dispatch_rotates_past_blocked_oldest_batch(archive_fixture):
    sessions, org, _ = archive_fixture
    jobs = [uuid4() for _ in range(4)]
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO archive_jobs(id,org_id,kind,generation,cutoff,state,inventory) VALUES(:id,:o,'audit',1,now(),'claimed','{}')"
            ),
            [dict(id=value, o=org) for value in jobs],
        )
        await db.commit()
        # Dedicated DB may retain exact failed test fixtures; filter verifies this
        # cohort rotates too by advancing bounded pages, no broad fixture cleanup.
        seen = set()
        for _ in range(20):
            rows = (await db.execute(text("SELECT * FROM platform_archive_pending(2)"))).all()
            seen.update(row.archive_id for row in rows)
            await db.commit()
            if set(jobs) <= seen:
                break
        assert set(jobs) <= seen


async def test_actual_app_login_cannot_assume_lifecycle_role(archive_fixture):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.engine import make_url
    from src.core.config import settings

    sessions, org, _ = archive_fixture
    role = "archive_app_test_" + uuid4().hex
    async with sessions() as db:
        await db.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD 'fixture-archive-app-only' IN ROLE aurum_ai_app"
            )
        )
        await db.commit()
    engine = create_async_engine(
        make_url(settings.DATABASE_URL).set(username=role, password="fixture-archive-app-only"),
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text("SET ROLE aurum_ai_lifecycle"))
    finally:
        await engine.dispose()
        async with sessions() as db:
            await db.execute(text(f"DROP ROLE {role}"))
            await db.commit()


async def test_unique_probe_catalogue_covers_all_alternate_indexes_and_is_operator_only(
    archive_fixture,
):
    from src.services.archive.collisions import UNIQUE_KEYS
    from src.services.archive.snapshot import TABLES

    sessions, org, _ = archive_fixture
    async with sessions() as db:
        indexes = (
            await db.execute(
                text(
                    "SELECT t.relname,array_agg(a.attname ORDER BY k.ordinality) AS columns FROM pg_index x JOIN pg_class t ON t.oid=x.indrelid CROSS JOIN LATERAL unnest(x.indkey) WITH ORDINALITY k(attnum,ordinality) JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum WHERE x.indisunique AND t.relnamespace='public'::regnamespace AND t.relname=ANY(:tables) GROUP BY t.relname,x.indexrelid"
                ),
                dict(tables=list(TABLES)),
            )
        ).all()
        actual = {(table, tuple(columns)) for table, columns in indexes if "id" not in columns}
        expected = {(table, columns) for table, keys in UNIQUE_KEYS.items() for columns in keys}
        assert actual == expected
        await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
        with pytest.raises(DBAPIError):
            await db.execute(
                text("SELECT platform_archive_unique_collision('file_metadata',:id,'{}'::jsonb)"),
                dict(id=uuid4()),
            )
    async with sessions() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_lifecycle"))
        with pytest.raises(DBAPIError):
            await db.execute(
                text("SELECT platform_archive_unique_collision('users',:id,'{}'::jsonb)"),
                dict(id=uuid4()),
            )
