"""Real app-role regressions for partition review findings."""

import asyncio
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import DBAPIError
from fastapi import HTTPException

from src.core.config import settings
from src.repositories.message_repository import message_repository


@pytest.fixture
async def partition_clients():
    url = (
        make_url(settings.DATABASE_URL)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    admin = await asyncpg.connect(url)
    org, user, conv, msg, agent, definition = [uuid.uuid4() for _ in range(6)]
    await admin.execute(
        "INSERT INTO organizations(id,name,slug) VALUES($1,'Partition review',$2)",
        org,
        f"partition-review-{org}",
    )
    await admin.execute(
        "INSERT INTO users(id,org_id,keycloak_id,email) VALUES($1,$2,$3,$4)",
        user,
        org,
        uuid.uuid4(),
        f"{user}@partition.test",
    )
    await admin.execute(
        "INSERT INTO conversations(id,org_id,user_id,model) VALUES($1,$2,$3,'test')",
        conv,
        org,
        user,
    )
    await admin.execute(
        "INSERT INTO messages(id,org_id,conversation_id,role) VALUES($1,$2,$3,'user')",
        msg,
        org,
        conv,
    )
    await admin.execute(
        "INSERT INTO agent_definitions(id,org_id,name,model_id,system_prompt) VALUES($1,$2,'Fixture','test','Test')",
        agent,
        org,
    )
    await admin.execute(
        "INSERT INTO workflow_definitions(id,org_id,name,supervisor_agent_definition_id) VALUES($1,$2,'Fixture',$3)",
        definition,
        org,
        agent,
    )
    first = await asyncpg.connect(
        url, server_settings={"role": "aurum_ai_app", "app.current_org_id": str(org)}
    )
    second = await asyncpg.connect(
        url, server_settings={"role": "aurum_ai_app", "app.current_org_id": str(org)}
    )
    try:
        yield (
            admin,
            first,
            second,
            dict(org=org, user=user, conv=conv, msg=msg, agent=agent, definition=definition),
        )
    finally:
        await first.close()
        await second.close()
        await admin.execute("DELETE FROM audit_log_identities WHERE org_id=$1", org)
        await admin.execute("DELETE FROM organizations WHERE id=$1", org)
        await admin.close()


@pytest.mark.asyncio
async def test_app_cannot_create_live_descendants_of_deleted_conversation(partition_clients):
    _, app, _, ids = partition_clients
    await app.execute("DELETE FROM conversations WHERE id=$1", ids["conv"])
    statements = [
        "INSERT INTO messages(id,org_id,conversation_id,role) VALUES($1,$2,$3,'user')",
        "INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,limits) VALUES($1,$2,$3,$4,'{}')",
        "INSERT INTO skills(id,org_id,owning_conversation_id,name,type) VALUES($1,$2,$3,'Owned','code')",
        "INSERT INTO knowledge_bases(id,org_id,owning_conversation_id,name) VALUES($1,$2,$3,'Owned')",
    ]
    for statement in statements:
        args = [uuid.uuid4(), ids["org"], ids["conv"]]
        if "owner_id,limits" in statement:
            args.append(ids["user"])
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await app.execute(statement, *args)
    engine = create_async_engine(
        settings.DATABASE_URL,
        connect_args={
            "server_settings": {"role": "aurum_ai_app", "app.current_org_id": str(ids["org"])}
        },
    )
    try:
        async with async_sessionmaker(engine)() as session:
            with pytest.raises((DBAPIError, ValueError, HTTPException)):
                await message_repository.upsert_assistant_by_turn(
                    session,
                    str(ids["conv"]),
                    uuid.uuid4(),
                    "assistant",
                    "orphan",
                    org_id=str(ids["org"]),
                )
            await session.rollback()
    finally:
        await engine.dispose()
    assert (
        await app.fetchval(
            "SELECT count(*) FROM message_identities WHERE conversation_id=$1", ids["conv"]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_app_cannot_retarget_owned_resources_to_deleted_conversation(partition_clients):
    _, app, _, ids = partition_clients
    live = uuid.uuid4()
    await app.execute(
        "INSERT INTO conversations(id,org_id,user_id,model) VALUES($1,$2,$3,'test')",
        live,
        ids["org"],
        ids["user"],
    )
    await app.execute("DELETE FROM conversations WHERE id=$1", ids["conv"])
    for table, insert in [
        (
            "skills",
            "INSERT INTO skills(id,org_id,owning_conversation_id,name,type) VALUES($1,$2,$3,'Owned','code')",
        ),
        (
            "knowledge_bases",
            "INSERT INTO knowledge_bases(id,org_id,owning_conversation_id,name) VALUES($1,$2,$3,'Owned')",
        ),
    ]:
        row = uuid.uuid4()
        await app.execute(insert, row, ids["org"], live)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await app.execute(
                f"UPDATE {table} SET owning_conversation_id=$1 WHERE id=$2", ids["conv"], row
            )
    turn = uuid.uuid4()
    await app.execute(
        "INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,limits) VALUES($1,$2,$3,$4,'{}')",
        turn,
        ids["org"],
        live,
        ids["user"],
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app.execute(
            "UPDATE interaction_turns SET conversation_id=$1 WHERE id=$2", ids["conv"], turn
        )


@pytest.mark.asyncio
async def test_app_workflow_requires_live_source_message(partition_clients):
    _, app, _, ids = partition_clients
    await app.execute("DELETE FROM messages WHERE id=$1", ids["msg"])
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app.execute(
            """INSERT INTO workflow_runs(id,org_id,workflow_definition_id,workflow_definition_version,
            initiating_user_id,conversation_id,turn_id,source_message_id,temporal_workflow_id,execution_snapshot)
            VALUES($1,$2,$3,1,$4,$5,$6,$7,$8,'{}')""",
            uuid.uuid4(),
            ids["org"],
            ids["definition"],
            ids["user"],
            ids["conv"],
            uuid.uuid4(),
            ids["msg"],
            str(uuid.uuid4()),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table",
    [
        "conversation_identities",
        "message_identities",
        "workflow_run_identities",
        "audit_log_identities",
    ],
)
@pytest.mark.parametrize(
    "column,value",
    [
        ("lifecycle", "'archived'"),
        ("generation", "99"),
        ("archive_id", "gen_random_uuid()"),
        ("lease_id", "gen_random_uuid()"),
        ("lease_until", "now()+interval '1 day'"),
    ],
)
async def test_app_registry_insert_cannot_forge_lifecycle(partition_clients, table, column, value):
    _, app, _, ids = partition_clients
    fields = "id,org_id,created_at"
    values = "$1,$2,now()"
    args = [uuid.uuid4(), ids["org"]]
    if table == "conversation_identities":
        fields += ",user_id"
        values += ",$3"
        args.append(ids["user"])
    elif table == "message_identities":
        fields += ",conversation_id,role"
        values += ",$3,'assistant'"
        args.append(ids["conv"])
    elif table == "workflow_run_identities":
        fields += (
            ",conversation_id,turn_id,initiating_user_id,source_message_id,temporal_workflow_id"
        )
        values += ",$3,$4,$5,$6,$7"
        args.extend([ids["conv"], uuid.uuid4(), ids["user"], ids["msg"], str(uuid.uuid4())])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute(f"INSERT INTO {table}({fields},{column}) VALUES({values},{value})", *args)


@pytest.mark.asyncio
async def test_default_writer_is_not_deadlock_victim_of_maintenance(partition_clients):
    admin, writer, maintenance, ids = partition_clients
    at = await admin.fetchval(
        "SELECT month FROM generate_series('1000-01-01'::timestamptz,'1800-01-01',interval '1 month') month WHERE to_regclass('public.conversations_p'||to_char(month,'YYYYMMDD')) IS NULL ORDER BY 1 LIMIT 1"
    )
    await writer.execute("BEGIN")
    await writer.execute("LOCK TABLE conversations IN ROW EXCLUSIVE MODE")
    maintenance_pid = maintenance.get_server_pid()

    async def maintain():
        await maintenance.execute("BEGIN")
        try:
            value = await maintenance.fetchval("SELECT platform_maintain_partitions()")
            await maintenance.execute("COMMIT")
            return value
        except Exception as error:
            await maintenance.execute("ROLLBACK")
            return error

    task = asyncio.create_task(maintain())
    # Wait for either a nonblocking answer (fixed code) or the exact old relation
    # wait while holding the advisory lock, then introduce the writer's trigger.
    for _ in range(200):
        if task.done() or await admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='relation' AND NOT granted)",
            maintenance_pid,
        ):
            break
        await asyncio.sleep(0.005)
    row = uuid.uuid4()

    async def write():
        try:
            await writer.execute(
                "INSERT INTO conversations(id,org_id,user_id,model,created_at) VALUES($1,$2,$3,'test',$4)",
                row,
                ids["org"],
                ids["user"],
                at,
            )
            await writer.execute("COMMIT")
            return "committed"
        except Exception as error:
            await writer.execute("ROLLBACK")
            return error

    written, maintained = await asyncio.wait_for(asyncio.gather(write(), task), 10)
    assert written == "committed"
    assert maintained is None
    assert await writer.fetchval("SELECT count(*) FROM conversations WHERE id=$1", row) == 1
    assert await maintenance.fetchval("SELECT platform_maintain_partitions()") == 1
    assert await writer.fetchval("SELECT count(*) FROM conversations WHERE id=$1", row) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_first", [True, False])
async def test_live_child_admission_serializes_with_parent_delete(partition_clients, writer_first):
    admin, writer, deleter, ids = partition_clients
    row = uuid.uuid4()
    await writer.execute("BEGIN")
    await deleter.execute("BEGIN")
    statement = "INSERT INTO messages(id,org_id,conversation_id,role) VALUES($1,$2,$3,'assistant')"
    if writer_first:
        await writer.execute(statement, row, ids["org"], ids["conv"])
        task = asyncio.create_task(
            deleter.execute("DELETE FROM conversations WHERE id=$1", ids["conv"])
        )
        waiter = deleter
    else:
        await deleter.execute("DELETE FROM conversations WHERE id=$1", ids["conv"])
        task = asyncio.create_task(writer.execute(statement, row, ids["org"], ids["conv"]))
        waiter = writer
    try:
        for _ in range(200):
            if task.done() or await admin.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)",
                waiter.get_server_pid(),
            ):
                break
            await asyncio.sleep(0.005)
        assert not task.done(), "Live-parent operation did not serialize on its payload row"
        if writer_first:
            await writer.execute("COMMIT")
            assert await asyncio.wait_for(task, 5) == "DELETE 1"
            await deleter.execute("COMMIT")
            assert (
                await admin.fetchval("SELECT count(*) FROM message_identities WHERE id=$1", row)
                == 1
            )
        else:
            await deleter.execute("COMMIT")
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await asyncio.wait_for(task, 5)
            await writer.execute("ROLLBACK")
            assert (
                await admin.fetchval("SELECT count(*) FROM message_identities WHERE id=$1", row)
                == 0
            )
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM messages WHERE conversation_id=$1", ids["conv"]
            )
            == 0
        )
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM conversation_identities WHERE id=$1", ids["conv"]
            )
            == 1
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_busy_maintenance_service_releases_locks_for_retry(partition_clients):
    from src.services.data_partitions import PartitionMaintenanceBusy, maintain_partitions

    admin, writer, _, ids = partition_clients
    at = await admin.fetchval(
        "SELECT month FROM generate_series('1000-01-01'::timestamptz,'1800-01-01',interval '1 month') month WHERE to_regclass('public.audit_logs_p'||to_char(month,'YYYYMMDD')) IS NULL ORDER BY 1 LIMIT 1"
    )
    engine = create_async_engine(
        settings.DATABASE_URL,
        connect_args={
            "server_settings": {"role": "aurum_ai_app", "app.current_org_id": str(ids["org"])}
        },
    )
    try:
        await writer.execute("BEGIN")
        await writer.execute("LOCK TABLE conversations IN ROW EXCLUSIVE MODE")
        async with async_sessionmaker(engine)() as session:
            with pytest.raises(PartitionMaintenanceBusy):
                await asyncio.wait_for(maintain_partitions(session), 5)
            # A deferred call must not retain the advisory lock in the caller's
            # still-open transaction, otherwise this normal default write hangs.
            row = uuid.uuid4()
            await asyncio.wait_for(
                writer.execute(
                    "INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES($1,$2,'POST','/fixture',200,$3)",
                    row,
                    ids["org"],
                    at,
                ),
                5,
            )
            await writer.execute("COMMIT")
            assert await maintain_partitions(session) == 1
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["conversation", "message", "workflow_run", "audit_log"])
async def test_app_payload_admission_uses_server_owned_registry_defaults(partition_clients, kind):
    _, app, _, ids = partition_clients
    row = uuid.uuid4()
    if kind == "conversation":
        await app.execute(
            "INSERT INTO conversations(id,org_id,user_id,model) VALUES($1,$2,$3,'test')",
            row,
            ids["org"],
            ids["user"],
        )
    elif kind == "message":
        await app.execute(
            "INSERT INTO messages(id,org_id,conversation_id,role) VALUES($1,$2,$3,'assistant')",
            row,
            ids["org"],
            ids["conv"],
        )
    elif kind == "workflow_run":
        await app.execute(
            """INSERT INTO workflow_runs(id,org_id,workflow_definition_id,workflow_definition_version,
            initiating_user_id,conversation_id,turn_id,source_message_id,temporal_workflow_id,execution_snapshot)
            VALUES($1,$2,$3,1,$4,$5,$6,$7,$8,'{}')""",
            row,
            ids["org"],
            ids["definition"],
            ids["user"],
            ids["conv"],
            uuid.uuid4(),
            ids["msg"],
            str(uuid.uuid4()),
        )
    else:
        await app.execute(
            "INSERT INTO audit_logs(id,org_id,method,path,status_code) VALUES($1,$2,'POST','/fixture',200)",
            row,
            ids["org"],
        )
    if kind == "message":
        engine = create_async_engine(
            settings.DATABASE_URL,
            connect_args={
                "server_settings": {
                    "role": "aurum_ai_app",
                    "app.current_org_id": str(ids["org"]),
                }
            },
        )
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                turn = uuid.uuid4()
                first = await message_repository.upsert_assistant_by_turn(
                    session,
                    str(ids["conv"]),
                    turn,
                    "assistant",
                    "first",
                    org_id=str(ids["org"]),
                    extra={"preserved": True},
                )
                second = await message_repository.upsert_assistant_by_turn(
                    session,
                    str(ids["conv"]),
                    turn,
                    "assistant",
                    "second",
                    extra={"merged": True},
                )
                assert second.id == first.id
                assert second.content == "second"
                assert second.extra == {"preserved": True, "merged": True}
                await session.commit()
        finally:
            await engine.dispose()
    registry = kind + "_identities"
    assert tuple(
        await app.fetchrow(
            f"SELECT lifecycle,generation,archive_id,lease_id,lease_until FROM {registry} WHERE id=$1",
            row,
        )
    ) == ("active", 1, None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_parent", ["messages", "audit_logs"])
async def test_first_parent_partition_ddl_never_waits_on_late_parent_writer(
    partition_clients, late_parent
):
    admin, writer, maintenance, ids = partition_clients
    at = await admin.fetchval(
        """SELECT month FROM generate_series('1000-01-01'::timestamptz,
        '1800-01-01',interval '1 month') month
        WHERE to_regclass('public.conversations_p'||to_char(month,'YYYYMMDD')) IS NULL
          AND to_regclass('public.'||$1||'_p'||to_char(month,'YYYYMMDD')) IS NULL
        ORDER BY 1 LIMIT 1""",
        late_parent,
    )
    seed = uuid.uuid4()
    await admin.execute(
        "INSERT INTO conversations(id,org_id,user_id,model,created_at) VALUES($1,$2,$3,'test',$4)",
        seed,
        ids["org"],
        ids["user"],
        at,
    )
    assert (
        await admin.fetchval("SELECT tableoid::regclass::text FROM conversations WHERE id=$1", seed)
        == "conversations_default"
    )
    await writer.execute("BEGIN")
    await writer.execute(f"LOCK TABLE {late_parent} IN ROW EXCLUSIVE MODE")

    async def maintain():
        await maintenance.execute("BEGIN")
        try:
            result = await maintenance.fetchval("SELECT platform_maintain_partitions()")
            await maintenance.execute("COMMIT")
            return result
        except Exception as error:
            await maintenance.execute("ROLLBACK")
            return error

    task = asyncio.create_task(maintain())
    blocked = False
    # Old code reaches all-parent security ALTER TABLE after creating the first
    # conversation month, before it checks the late parent's NOWAIT lock.
    for _ in range(400):
        if task.done():
            break
        blocked = await admin.fetchval(
            """SELECT EXISTS(SELECT 1 FROM pg_locks l
            JOIN pg_class c ON c.oid=l.relation WHERE l.pid=$1 AND NOT l.granted
            AND c.relname LIKE $2)""",
            maintenance.get_server_pid(),
            late_parent + "_%",
        )
        if blocked:
            break
        await asyncio.sleep(0.005)
    assert task.done() or blocked
    row = uuid.uuid4()

    async def write():
        try:
            if late_parent == "messages":
                await writer.execute(
                    "INSERT INTO messages(id,org_id,conversation_id,role,created_at) VALUES($1,$2,$3,'assistant',$4)",
                    row,
                    ids["org"],
                    ids["conv"],
                    at,
                )
            else:
                await writer.execute(
                    "INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES($1,$2,'POST','/fixture',200,$3)",
                    row,
                    ids["org"],
                    at,
                )
            await writer.execute("COMMIT")
            return "committed"
        except Exception as error:
            await writer.execute("ROLLBACK")
            return error

    written, maintained = await asyncio.wait_for(asyncio.gather(write(), task), 10)
    assert written == "committed"
    assert maintained is None
    assert (
        await admin.fetchval("SELECT tableoid::regclass::text FROM conversations WHERE id=$1", seed)
        == "conversations_default"
    )
    assert await maintenance.fetchval("SELECT platform_maintain_partitions()") == 2
    assert await admin.fetchval("SELECT count(*) FROM conversations WHERE id=$1", seed) == 1
    assert await admin.fetchval(f"SELECT count(*) FROM {late_parent} WHERE id=$1", row) == 1
