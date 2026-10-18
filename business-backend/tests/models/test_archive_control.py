"""Real SQL lifecycle fencing, including direct partition writes."""

# ruff: noqa: F811
import uuid

import asyncpg
import pytest

from tests.models.test_partition_review import partition_clients  # noqa: F401, F811


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table,assignment", [("conversations", "title='changed'"), ("messages", "content='changed'")]
)
async def test_frozen_payload_rejects_direct_updates(partition_clients, table, assignment):
    admin, app, _, ids = partition_clients
    await admin.execute(
        "UPDATE conversation_identities SET lifecycle='archiving' WHERE id=$1", ids["conv"]
    )
    row = ids["conv"] if table == "conversations" else ids["msg"]
    child = await admin.fetchval(f"SELECT tableoid::regclass::text FROM {table} WHERE id=$1", row)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app.execute(f"UPDATE {child} SET {assignment} WHERE id=$1", row)


@pytest.mark.asyncio
async def test_app_cannot_forge_terminal_receipt(partition_clients):
    _, app, _, ids = partition_clients
    assert not await app.fetchval("SELECT to_regclass('conversation_operations') IS NULL")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute(
            "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,tree_complete) VALUES($1,$2,$3,1,'default','x',$4,'ConversationWorkflow','x',true)",
            uuid.uuid4(),
            ids["org"],
            ids["conv"],
            uuid.uuid4(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_first", [True, False])
async def test_direct_child_update_and_archive_claim_cannot_deadlock(
    partition_clients, writer_first
):
    import asyncio

    admin, writer, claimant, ids = partition_clients
    await claimant.execute("SET ROLE postgres")
    await writer.execute("BEGIN")
    await claimant.execute("BEGIN")

    async def claim():
        await claimant.execute(
            "SELECT 1 FROM conversations WHERE id=$1 FOR NO KEY UPDATE", ids["conv"]
        )
        await claimant.execute(
            "UPDATE conversation_identities SET lifecycle='archiving' WHERE id=$1", ids["conv"]
        )

    try:
        if writer_first:
            await writer.execute("UPDATE messages SET content='writer' WHERE id=$1", ids["msg"])
            task = asyncio.create_task(claim())
            for _ in range(100):
                if await admin.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)",
                    claimant.get_server_pid(),
                ):
                    break
                await asyncio.sleep(0.005)
            assert not task.done()
            await writer.execute("COMMIT")
            await asyncio.wait_for(task, 5)
        else:
            await claim()
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await asyncio.wait_for(
                    writer.execute("UPDATE messages SET content='late' WHERE id=$1", ids["msg"]), 5
                )
            await writer.execute("ROLLBACK")
        await claimant.execute("COMMIT")
    finally:
        await writer.execute("ROLLBACK")
        await claimant.execute("ROLLBACK")


@pytest.mark.asyncio
async def test_frozen_owned_resource_cannot_remove_owner_to_bypass_guard(partition_clients):
    admin, app, _, ids = partition_clients
    kb = uuid.uuid4()
    await app.execute(
        "INSERT INTO knowledge_bases(id,org_id,owning_conversation_id,name) VALUES($1,$2,$3,'owned')",
        kb,
        ids["org"],
        ids["conv"],
    )
    await admin.execute(
        "UPDATE conversation_identities SET lifecycle='archiving' WHERE id=$1", ids["conv"]
    )
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app.execute("UPDATE knowledge_bases SET owning_conversation_id=NULL WHERE id=$1", kb)


@pytest.mark.asyncio
async def test_internal_transaction_hook_rechecks_generation_after_commit(partition_clients):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from src.core.config import settings
    from src.core.database import _AppSession
    from src.services.conversation_lifecycle import ConversationFrozen

    admin, _, _, ids = partition_clients
    engine = create_async_engine(
        settings.DATABASE_URL, connect_args={"server_settings": {"role": "aurum_ai_app"}}
    )
    try:
        async with async_sessionmaker(engine, sync_session_class=_AppSession)() as session:
            session.info["org_id"] = str(ids["org"])
            session.info["conversation_fence"] = (ids["org"], ids["conv"], 1)
            assert await session.scalar(text("SELECT 1")) == 1
            await session.commit()
            await admin.execute(
                "UPDATE conversation_identities SET generation=3,restored_at=now() WHERE id=$1",
                ids["conv"],
            )
            with pytest.raises(ConversationFrozen):
                await session.scalar(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_operator_role_preserves_tenant_rls_and_app_cannot_assume_membership(
    partition_clients,
):
    admin, _, operator, ids = partition_clients
    assert not await admin.fetchval(
        "SELECT pg_has_role('aurum_ai_app','aurum_ai_lifecycle','MEMBER')"
    )
    assert not await admin.fetchval(
        "SELECT has_function_privilege('aurum_ai_app','discover_pending_conversation_executions(integer)','EXECUTE')"
    )
    await operator.execute("SET ROLE aurum_ai_lifecycle")
    assert (
        await operator.fetchval("SELECT count(*) FROM conversations WHERE id=$1", ids["conv"]) == 1
    )
    await operator.execute("SELECT set_config('app.current_org_id',$1,false)", str(uuid.uuid4()))
    assert (
        await operator.fetchval("SELECT count(*) FROM conversations WHERE id=$1", ids["conv"]) == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["detach", "retarget", "source"])
async def test_file_metadata_explicit_owner_and_source_are_immutable(partition_clients, mutation):
    _, app, _, ids = partition_clients
    other, kb, other_kb, file_id = [uuid.uuid4() for _ in range(4)]
    await app.execute(
        "INSERT INTO conversations(id,org_id,user_id,model) VALUES($1,$2,$3,'test')",
        other,
        ids["org"],
        ids["user"],
    )
    for base, owner in ((kb, ids["conv"]), (other_kb, other)):
        await app.execute(
            "INSERT INTO knowledge_bases(id,org_id,owning_conversation_id,name) VALUES($1,$2,$3,$4)",
            base,
            ids["org"],
            owner,
            str(base),
        )
    source = f"documents/{ids['org']}/{kb}/original.txt"
    await app.execute(
        "INSERT INTO file_metadata(id,org_id,storage_key,filename,size_bytes) VALUES($1,$2,$3,'original.txt',1)",
        file_id,
        ids["org"],
        source,
    )
    before = await app.fetchrow(
        "SELECT owning_conversation_id,storage_key FROM file_metadata WHERE id=$1", file_id
    )
    assert before["owning_conversation_id"] == ids["conv"]
    try:
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            if mutation == "source":
                await app.execute(
                    "UPDATE file_metadata SET storage_key=$2 WHERE id=$1",
                    file_id,
                    f"documents/{ids['org']}/{other_kb}/original.txt",
                )
            else:
                await app.execute(
                    "UPDATE file_metadata SET owning_conversation_id=$2 WHERE id=$1",
                    file_id,
                    None if mutation == "detach" else other,
                )
        assert (
            await app.fetchrow(
                "SELECT owning_conversation_id,storage_key FROM file_metadata WHERE id=$1", file_id
            )
            == before
        )
    finally:
        await app.execute("DELETE FROM file_metadata WHERE id=$1", file_id)


@pytest.mark.asyncio
async def test_direct_sql_kb_delete_cannot_orphan_unsettled_attachment(partition_clients):
    admin, app, _, ids = partition_clients
    kb_id, operation_id = uuid.uuid4(), uuid.uuid4()
    await app.execute(
        "INSERT INTO knowledge_bases(id,org_id,owning_conversation_id,name) VALUES($1,$2,$3,'pending')",
        kb_id,
        ids["org"],
        ids["conv"],
    )
    await app.execute(
        "INSERT INTO conversation_operations(id,org_id,conversation_id,generation,kind,operation_key,knowledge_base_id) VALUES($1,$2,$3,1,'document_create',$4,$5)",
        operation_id,
        ids["org"],
        ids["conv"],
        uuid.uuid4(),
        kb_id,
    )
    try:
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await app.execute("DELETE FROM knowledge_bases WHERE id=$1", kb_id)
        assert await app.fetchval("SELECT EXISTS(SELECT 1 FROM knowledge_bases WHERE id=$1)", kb_id)
    finally:
        await admin.execute("DELETE FROM conversation_operations WHERE id=$1", operation_id)
