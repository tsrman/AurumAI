from uuid import uuid4
from datetime import datetime, timedelta, timezone
import importlib.util
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.core.config import settings


@pytest.fixture
async def archive_fixture():
    engine = create_async_engine(settings.DATABASE_URL, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, user, conversation = uuid4(), uuid4(), uuid4()
    old = datetime.now(timezone.utc) - timedelta(days=181)
    async with sessions() as s:
        await s.execute(
            text("INSERT INTO organizations(id,name,slug) VALUES(:o,'archive test',:slug)"),
            dict(o=org, slug=str(org)),
        )
        await s.execute(
            text("INSERT INTO users(id,org_id,keycloak_id,email) VALUES(:u,:o,:k,:e)"),
            dict(u=user, o=org, k=uuid4(), e=f"{user}@test.invalid"),
        )
        await s.execute(text("SELECT platform_ensure_partition('conversations',:at)"), dict(at=old))
        await s.execute(
            text(
                "INSERT INTO conversations(id,org_id,user_id,created_at,updated_at,model,title,status,cost_usd) VALUES(:c,:o,:u,:at,:at,'test','old','deleted',0.123456)"
            ),
            dict(c=conversation, o=org, u=user, at=old),
        )
        await s.commit()
    yield sessions, org, conversation
    async with sessions() as s:
        # Only this fixture's exact tenant; all external objects use its random UUID.
        for table in (
            "file_metadata",
            "conversation_executions",
            "conversation_operations",
            "llm_usage_events",
            "web_sources",
            "interaction_outbox",
            "user_questions",
            "tool_operations",
            "interaction_executions",
            "interaction_turns",
            "workflow_node_runs",
            "workflow_runs",
            "workflow_definitions",
            "agent_definitions",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE org_id=:o"), dict(o=org))
        await s.execute(text("DELETE FROM organizations WHERE id=:o"), dict(o=org))
        await s.commit()
    await engine.dispose()


async def test_real_business_archive_roundtrip(archive_fixture, tmp_path):
    assert (
        importlib.util.find_spec("src.services.archive.service") is not None
    ), "archive service missing"
    from src.services.archive.service import ArchiveService

    sessions, org, conversation = archive_fixture
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    result = await service.run(org, conversation, apply=False)
    assert result["eligible"] is True
    result = await service.run(org, conversation, apply=True)
    archive_id = result["archive_id"]
    async with sessions() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM conversations WHERE id=:c"), dict(c=conversation)
            )
            == 0
        )
        assert (
            await s.scalar(
                text("SELECT lifecycle FROM conversation_identities WHERE id=:c"),
                dict(c=conversation),
            )
            == "archived"
        )
    await service.restore(org, archive_id, apply=False)
    await service.restore(org, archive_id, apply=True)
    await service.restore(org, archive_id, apply=True)
    async with sessions() as s:
        row = (
            await s.execute(
                text("SELECT title,status,cost_usd FROM conversations WHERE id=:c"),
                dict(c=conversation),
            )
        ).one()
        assert tuple(map(str, row)) == ("old", "deleted", "0.123456")
        assert (
            await s.scalar(
                text("SELECT generation FROM conversation_identities WHERE id=:c"),
                dict(c=conversation),
            )
            == 3
        )
    result = await service.run(org, conversation, apply=False)
    assert result["blocked_reason"] == "recent_activity"


class LocalArchiveStorage:
    def __init__(self, root):
        self.root = root

    async def upload(self, key, path):
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(path.read_bytes())

    async def download(self, key, path):
        path.write_bytes((self.root / key).read_bytes())
        path.chmod(0o600)

    async def healthy(self):
        return True


@pytest.mark.parametrize("boundary", ["_seal", "_purge", "_stage"])
async def test_crash_after_committed_boundary_is_recoverable(
    archive_fixture, tmp_path, monkeypatch, boundary
):
    from src.services.archive.service import ArchiveService

    sessions, org, conversation = archive_fixture
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    original = getattr(service, boundary)

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("test_process_loss")

    if boundary == "_stage":
        result = await service.run(org, conversation, apply=True)
        archive = result["archive_id"]
        monkeypatch.setattr(service, boundary, crash)
        with pytest.raises(RuntimeError, match="test_process_loss"):
            await service.restore(org, archive, apply=True)
        monkeypatch.setattr(service, boundary, original)
        await service.restore(org, archive, apply=True)
        assert (await service.list(org))[0]["state"] == "restored"
    else:
        monkeypatch.setattr(service, boundary, crash)
        with pytest.raises(RuntimeError, match="test_process_loss"):
            await service.run(org, conversation, apply=True)
        archive = (await service.list(org))[0]["archive_id"]
        monkeypatch.setattr(service, boundary, original)
        from uuid import UUID

        await service.resume(org, UUID(archive))
        assert (await service.list(org))[0]["state"] == "purged"


async def test_corrupt_uploaded_part_keeps_business_payload(archive_fixture, tmp_path):
    from src.services.archive.service import ArchiveService
    from src.services.conversation_lifecycle import ArchiveBlocked

    class Corrupt(LocalArchiveStorage):
        async def upload(self, key, path):
            await super().upload(key, path)
            (self.root / key).write_bytes(b"corrupt")

    sessions, org, conversation = archive_fixture
    service = ArchiveService(sessions, Corrupt(tmp_path / "objects"))
    with pytest.raises(ArchiveBlocked, match="readback_mismatch"):
        await service.run(org, conversation, apply=True)
    async with sessions() as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM conversations WHERE id=:c"), dict(c=conversation)
            )
            == 1
        )
        assert (
            await s.scalar(text("SELECT state FROM archive_jobs WHERE org_id=:o"), dict(o=org))
            == "claimed"
        )


async def test_audit_own_batch_preserves_new_insert_and_restores(archive_fixture, tmp_path):
    from src.services.archive.service import ArchiveService

    sessions, org, _ = archive_fixture
    old, new = uuid4(), uuid4()
    at = datetime.now(timezone.utc) - timedelta(days=181)
    async with sessions() as s:
        await s.execute(text("SELECT platform_ensure_partition('audit_logs',:at)"), dict(at=at))
        await s.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,created_at,updated_at,method,path,status_code) VALUES(:id,:o,:at,:at,'POST','/fixture',200)"
            ),
            dict(id=old, o=org, at=at),
        )
        await s.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,method,path,status_code) VALUES(:id,:o,'POST','/new',200)"
            ),
            dict(id=new, o=org),
        )
        await s.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    result = await service.run_audit(org, apply=True)
    async with sessions() as s:
        assert (
            await s.scalars(text("SELECT id FROM audit_logs WHERE org_id=:o"), dict(o=org))
        ).all() == [new]
    await service.restore(org, result["archive_id"], apply=True)
    async with sessions() as s:
        assert set(
            (await s.scalars(text("SELECT id FROM audit_logs WHERE org_id=:o"), dict(o=org))).all()
        ) == {old, new}


async def test_old_conversation_recent_activity_and_override(archive_fixture, tmp_path):
    from src.services.archive.service import ArchiveService

    sessions, org, conversation = archive_fixture
    service = ArchiveService(
        sessions, LocalArchiveStorage(tmp_path / "objects"), retention_days=182
    )
    assert (await service.run(org, conversation))["blocked_reason"] == "recent_activity"
    service.retention_days = 180
    async with sessions() as s:
        await s.execute(
            text("UPDATE conversations SET last_message_at=now() WHERE id=:c"), dict(c=conversation)
        )
        await s.commit()
    assert (await service.run(org, conversation))["blocked_reason"] == "recent_activity"


async def test_restore_dry_run_rejects_dependency_uuid_collision(archive_fixture, tmp_path):
    from src.services.archive.service import ArchiveService
    from src.services.conversation_lifecycle import ArchiveBlocked

    sessions, org, conversation = archive_fixture
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    result = await service.run(org, conversation, apply=True)
    async with service.session(org) as s:
        await s.execute(
            text(
                "INSERT INTO conversations(id,org_id,user_id,created_at,updated_at,model,title) SELECT id,org_id,user_id,created_at,created_at,'different','collision' FROM conversation_identities WHERE id=:c"
            ),
            dict(c=conversation),
        )
        await s.commit()
    with pytest.raises(ArchiveBlocked, match="restore_payload_collision"):
        await service.restore(org, result["archive_id"], apply=False)


async def test_unsettled_billing_request_blocks_archive(archive_fixture, tmp_path):
    from src.services.archive.service import ArchiveService

    sessions, org, conversation = archive_fixture
    async with sessions() as s:
        await s.execute(
            text(
                "INSERT INTO llm_usage_events(id,org_id,user_id,conversation_id,request_id,period_start,model,status) SELECT :id,org_id,user_id,id,:request,current_date,'test','accepted' FROM conversations WHERE id=:c"
            ),
            dict(id=uuid4(), request=uuid4(), c=conversation),
        )
        await s.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    assert (await service.run(org, conversation))["blocked_reason"] == "unsettled_billing_request"


async def test_json_literal_null_survives_staged_restore_retry(
    archive_fixture, tmp_path, monkeypatch
):
    from src.services.archive.service import ArchiveService

    sessions, org, conversation = archive_fixture
    async with sessions() as s:
        await s.execute(
            text("UPDATE conversations SET extra='null'::json WHERE id=:c"), dict(c=conversation)
        )
        await s.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    result = await service.run(org, conversation, apply=True)
    original = service._stage

    async def crash(*args):
        await original(*args)
        raise RuntimeError("test_lost_stage_reply")

    monkeypatch.setattr(service, "_stage", crash)
    with pytest.raises(RuntimeError, match="test_lost_stage_reply"):
        await service.restore(org, result["archive_id"], apply=True)
    monkeypatch.setattr(service, "_stage", original)
    await service.restore(org, result["archive_id"], apply=True)
    async with sessions() as s:
        assert (
            await s.scalar(
                text("SELECT extra::text FROM conversations WHERE id=:c"), dict(c=conversation)
            )
            == "null"
        )
        assert (
            await s.scalar(
                text("SELECT system_prompt IS NULL FROM conversations WHERE id=:c"),
                dict(c=conversation),
            )
            is True
        )


async def test_unknown_message_content_reference_blocks_export(archive_fixture, tmp_path):
    from src.services.archive.service import ArchiveService
    from src.services.archive.schemas import ArchiveFormatError

    sessions, org, conversation = archive_fixture
    async with sessions() as db:
        await db.execute(
            text(
                "SELECT platform_ensure_partition('messages',created_at) FROM conversations WHERE id=:c"
            ),
            dict(c=conversation),
        )
        await db.execute(
            text(
                "INSERT INTO messages(id,org_id,conversation_id,role,content_ref,created_at,updated_at) SELECT :m,org_id,id,'assistant','legacy/external/object',created_at,created_at FROM conversations WHERE id=:c"
            ),
            dict(m=uuid4(), c=conversation),
        )
        await db.execute(
            text(
                "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete) VALUES(:id,:o,:c,1,'default',:wf,:r,'ConversationWorkflow','test','COMPLETED',now(),now(),true)"
            ),
            dict(id=uuid4(), o=org, c=conversation, wf=f"archive-ref-{conversation}", r=uuid4()),
        )
        await db.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    with pytest.raises(ArchiveFormatError, match="external_message_reference_unsupported"):
        await service.run(org, conversation, apply=True)


async def test_orphan_owned_attachment_never_purges_without_rag_inventory(
    archive_fixture, tmp_path
):
    from src.services.archive.service import ArchiveService
    from src.services.archive.schemas import ArchiveFormatError

    sessions, org, conversation = archive_fixture
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO file_metadata(id,org_id,owning_conversation_id,storage_key,filename,size_bytes) VALUES(:id,:o,:c,'legacy/source','legacy.txt',12)"
            ),
            dict(id=uuid4(), o=org, c=conversation),
        )
        await db.commit()
    with pytest.raises(ArchiveFormatError, match="attachment_inventory_unknown"):
        await ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects")).run(
            org, conversation, apply=True
        )


async def test_exact_audit_job_retry_never_selects_next_batch(archive_fixture, tmp_path):
    from src.services.archive.service import ArchiveService

    sessions, org, _ = archive_fixture
    archive = uuid4()
    first, second = uuid4(), uuid4()
    at = datetime(2025, 2, 1, tzinfo=timezone.utc)
    async with sessions() as db:
        await db.execute(text("SELECT platform_ensure_partition('audit_logs',:at)"), dict(at=at))
        await db.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,created_at,method,path,status_code) VALUES(:id,:o,:at,'POST','/fixture',200)"
            ),
            dict(id=first, o=org, at=at),
        )
        await db.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    await service.run_audit(org, apply=True, archive_id=archive, batch_size=1)
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,created_at,method,path,status_code) VALUES(:id,:o,:at,'POST','/fixture',200)"
            ),
            dict(id=second, o=org, at=at),
        )
        await db.commit()
    await service.run_audit(org, apply=True, archive_id=archive, batch_size=1)
    async with sessions() as db:
        assert (
            await db.scalars(text("SELECT id FROM audit_logs WHERE org_id=:o"), dict(o=org))
        ).all() == [second]


async def test_exact_180_day_boundary_is_not_eligible(archive_fixture, tmp_path, monkeypatch):
    from src.services.archive import service as module

    sessions, org, conversation = archive_fixture
    now = datetime.now(timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(module, "datetime", Clock)
    service = module.ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    async with sessions() as db:
        await db.execute(
            text("UPDATE conversations SET updated_at=:at WHERE id=:c"),
            dict(c=conversation, at=now - timedelta(days=180)),
        )
        await db.commit()
    assert (await service.run(org, conversation))["blocked_reason"] == "recent_activity"
    async with sessions() as db:
        await db.execute(
            text("UPDATE conversations SET updated_at=:at WHERE id=:c"),
            dict(c=conversation, at=now - timedelta(days=180, microseconds=1)),
        )
        await db.commit()
    assert (await service.run(org, conversation))["eligible"] is True


@pytest.mark.parametrize(
    "address", ["192.0.2.15", "2001:db8::15", "192.0.2.15/24", "2001:db8::15/64"]
)
async def test_audit_native_inet_roundtrip(archive_fixture, tmp_path, address):
    from src.services.archive.service import ArchiveService

    sessions, org, _ = archive_fixture
    audit = uuid4()
    at = datetime(2025, 2, 1, tzinfo=timezone.utc)
    async with sessions() as db:
        await db.execute(text("SELECT platform_ensure_partition('audit_logs',:at)"), dict(at=at))
        await db.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,created_at,method,path,status_code,ip_address) VALUES(:id,:o,:at,'POST','/fixture',200,CAST(:ip AS inet))"
            ),
            dict(id=audit, o=org, at=at, ip=address),
        )
        await db.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    result = await service.run_audit(org, apply=True)
    await service.restore(org, result["archive_id"], apply=True)
    async with sessions() as db:
        assert (
            str(
                await db.scalar(
                    text("SELECT ip_address FROM audit_logs WHERE id=:id"), dict(id=audit)
                )
            )
            == address
        )


@pytest.mark.parametrize("phase", ["purge", "restore"])
async def test_audit_changed_epoch_blocks_payload_mutation(
    archive_fixture, tmp_path, monkeypatch, phase
):
    from src.services.archive.service import ArchiveService, ArchiveBlocked

    sessions, org, _ = archive_fixture
    audit = uuid4()
    at = datetime.now(timezone.utc) - timedelta(days=181)
    async with sessions() as db:
        await db.execute(text("SELECT platform_ensure_partition('audit_logs',:at)"), dict(at=at))
        await db.execute(
            text(
                "INSERT INTO audit_logs(id,org_id,created_at,method,path,status_code) VALUES(:id,:o,:at,'POST','/fixture',200)"
            ),
            dict(id=audit, o=org, at=at),
        )
        await db.commit()
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))

    async def change_epoch():
        async with sessions() as db:
            await db.execute(
                text(
                    "UPDATE audit_log_identities SET generation=generation+1 WHERE id=:id AND org_id=:o"
                ),
                dict(id=audit, o=org),
            )
            await db.commit()

    if phase == "purge":
        original = service._purge

        async def stale_purge(owner, job):
            await change_epoch()
            await original(owner, job)

        monkeypatch.setattr(service, "_purge", stale_purge)
        with pytest.raises(ArchiveBlocked, match="audit_identity_changed"):
            await service.run_audit(org, apply=True)
    else:
        result = await service.run_audit(org, apply=True)
        await change_epoch()
        with pytest.raises(ArchiveBlocked, match="audit_identity_changed"):
            await service.restore(org, result["archive_id"], apply=True)
    async with sessions() as db:
        count = await db.scalar(
            text("SELECT count(*) FROM audit_logs WHERE id=:id"), dict(id=audit)
        )
        assert count == (1 if phase == "purge" else 0)


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("size", [1024 * 1024 + 1, 2 * 1024 * 1024])
async def test_oversized_legacy_cell_rejected_before_claim(archive_fixture, tmp_path, apply, size):
    from src.services.archive.service import ArchiveService
    from src.services.archive.schemas import ArchiveFormatError
    from src.services.rag_archive_client import RagArchiveInspection

    sessions, org, conversation = archive_fixture
    at = datetime.now(timezone.utc) - timedelta(days=181)
    async with sessions() as db:
        await db.execute(text("SELECT platform_ensure_partition('messages',:at)"), dict(at=at))
        await db.execute(
            text(
                "INSERT INTO messages(id,org_id,conversation_id,role,content,created_at,updated_at) VALUES(:id,:o,:c,'assistant',repeat('x',:size),:at,:at)"
            ),
            dict(id=uuid4(), o=org, c=conversation, at=at, size=size),
        )
        # Format test uses a complete synthetic terminal receipt; Temporal transport
        # is covered by the dedicated real worker tests.
        await db.execute(
            text(
                "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete) VALUES(:id,:o,:c,1,'default',:wf,:run,'ConversationWorkflow','test','COMPLETED',:at,:at,true)"
            ),
            dict(id=uuid4(), o=org, c=conversation, wf=str(uuid4()), run=uuid4(), at=at),
        )
        await db.execute(
            text(
                "INSERT INTO knowledge_bases(id,org_id,name,owning_conversation_id) VALUES(:id,:o,'oversized fixture',:c)"
            ),
            dict(id=uuid4(), o=org, c=conversation),
        )
        await db.commit()

    class NoStorageCalls(LocalArchiveStorage):
        async def upload(self, *args):
            raise AssertionError("storage called before admission")

        async def download(self, *args):
            raise AssertionError("storage called before admission")

    class NoRagMutations:
        async def inspect(self, **kwargs):
            return RagArchiveInspection(state="active", generation=1, archive_id=None)

        async def command(self, *args, **kwargs):
            raise AssertionError("RAG mutation called before admission")

    service = ArchiveService(sessions, NoStorageCalls(tmp_path / "objects"), NoRagMutations())
    with pytest.raises(ArchiveFormatError):
        await service.run(org, conversation, apply=apply)
    async with sessions() as db:
        assert (
            await db.scalar(
                text("SELECT lifecycle FROM conversation_identities WHERE id=:c"),
                dict(c=conversation),
            )
            == "active"
        )
        assert (
            await db.scalar(text("SELECT count(*) FROM archive_jobs WHERE org_id=:o"), dict(o=org))
            == 0
        )
        assert (
            await db.scalar(
                text("SELECT generation FROM conversation_identities WHERE id=:c"),
                dict(c=conversation),
            )
            == 1
        )


@pytest.mark.parametrize("apply", [False, True])
async def test_slow_rag_inspection_does_not_hold_conversation_write_lock(
    archive_fixture, tmp_path, apply
):
    import asyncio
    from src.services.archive.service import ArchiveService
    from src.services.rag_archive_client import RagArchiveInspection

    sessions, org, conversation = archive_fixture
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO knowledge_bases(id,org_id,name,owning_conversation_id) VALUES(:id,:o,'slow inspect',:c)"
            ),
            dict(id=uuid4(), o=org, c=conversation),
        )
        await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowRag:
        async def inspect(self, **kwargs):
            entered.set()
            await release.wait()
            return RagArchiveInspection(state="active", generation=1, archive_id=None)

    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"), SlowRag())
    task = asyncio.create_task(service.run(org, conversation, apply=apply))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with sessions() as db:
            await db.execute(text("SET LOCAL lock_timeout='200ms'"))
            await db.execute(
                text(
                    "UPDATE conversations SET title='concurrent writer',updated_at=now() WHERE id=:c"
                ),
                dict(c=conversation),
            )
            await db.commit()
    finally:
        release.set()
        result = await asyncio.wait_for(task, timeout=5)
    assert result == {"eligible": False, "blocked_reason": "recent_activity"}
    async with sessions() as db:
        assert (
            await db.scalar(
                text("SELECT generation FROM conversation_identities WHERE id=:c"),
                dict(c=conversation),
            )
            == 1
        )
        assert (
            await db.scalar(text("SELECT count(*) FROM archive_jobs WHERE org_id=:o"), dict(o=org))
            == 0
        )


async def test_max_cell_boundary_roundtrip_bounds_actual_sql_page_bytes(archive_fixture, tmp_path):
    from sqlalchemy import event
    from src.services.archive.service import ArchiveService
    from src.services.archive.schemas import MAX_CELL, MAX_PART

    sessions, org, conversation = archive_fixture
    at = datetime.now(timezone.utc) - timedelta(days=181)
    async with sessions() as db:
        await db.execute(text("SELECT platform_ensure_partition('messages',:at)"), dict(at=at))
        await db.execute(
            text(
                "INSERT INTO messages(id,org_id,conversation_id,role,content,created_at,updated_at) VALUES(:id,:o,:c,'assistant',repeat('x',:size),:at,:at)"
            ),
            [dict(id=uuid4(), o=org, c=conversation, size=MAX_CELL, at=at) for _ in range(12)],
        )
        await db.execute(
            text(
                "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete) VALUES(:id,:o,:c,1,'default',:wf,:run,'ConversationWorkflow','test','COMPLETED',:at,:at,true)"
            ),
            dict(id=uuid4(), o=org, c=conversation, wf=str(uuid4()), run=uuid4(), at=at),
        )
        await db.commit()
    engine = sessions.kw["bind"].sync_engine
    limits = []

    def check_sql_page(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().startswith("SELECT")
            and "messages.content" in statement
            and "LIMIT" in statement
            and "max(" not in statement
        ):
            # Observe the actual SQL sent to PostgreSQL before any rows transfer.
            limit = context.compiled.statement._limit_clause.value
            limits.append(limit)
            assert limit * MAX_CELL <= MAX_PART // 2

    event.listen(engine, "before_cursor_execute", check_sql_page)
    try:
        service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
        result = await service.run(org, conversation, apply=True)
        await service.restore(org, result["archive_id"], apply=True)
    finally:
        event.remove(engine, "before_cursor_execute", check_sql_page)
    assert limits and max(limits) <= 3  # row metadata consumes part of the 4 MiB budget
    async with sessions() as db:
        assert (
            await db.execute(
                text(
                    "SELECT count(*),min(octet_length(content)),max(octet_length(content)) FROM messages WHERE org_id=:o"
                ),
                dict(o=org),
            )
        ).one() == (12, MAX_CELL, MAX_CELL)
