"""Real PostgreSQL contracts for permanent identity and monthly payloads."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.models.conversation import Conversation
from src.models.message import Message


@pytest.mark.asyncio
async def test_payloads_are_monthly_partitions_with_secured_children(db):
    parents = (
        (
            await db.execute(
                text("""
        SELECT c.relname FROM pg_partitioned_table p JOIN pg_class c ON c.oid=p.partrelid
        WHERE c.relname IN ('conversations','messages','audit_logs') ORDER BY 1
    """)
            )
        )
        .scalars()
        .all()
    )
    assert parents == ["audit_logs", "conversations", "messages"]
    assert (
        await db.execute(
            text("""
        SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent
        JOIN pg_class c ON c.oid=i.inhrelid
        WHERE p.relname IN ('conversations','messages','audit_logs')
        AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
    """)
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_global_uuid_and_natural_key_survive_month_boundaries(db, test_org, test_user):
    conversation = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conversation)
    await db.flush()
    identifier, turn = uuid.uuid4(), uuid.uuid4()
    at = datetime(2024, 1, 31, 23, 59, tzinfo=timezone.utc)
    db.add(
        Message(
            id=identifier,
            org_id=test_org.id,
            conversation_id=conversation.id,
            turn_id=turn,
            role="assistant",
            content="exact payload",
            created_at=at,
        )
    )
    await db.flush()
    for duplicate_id in [identifier, uuid.uuid4()]:
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    text("""
                    INSERT INTO messages(id,org_id,conversation_id,turn_id,role,created_at)
                    VALUES(:id,:org,:conv,:turn,'assistant','2024-02-01T00:00:00Z')
                """),
                    dict(id=duplicate_id, org=test_org.id, conv=conversation.id, turn=turn),
                )
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE messages SET created_at=created_at+interval '1 month' WHERE id=:id"),
                {"id": identifier},
            )
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE message_identities SET role='user' WHERE id=:id"), {"id": identifier}
            )
    assert (
        await db.execute(text("SELECT content FROM messages WHERE id=:id"), {"id": identifier})
    ).scalar_one() == "exact payload"


@pytest.mark.asyncio
async def test_default_drain_preserves_graph_and_exact_payload(db, test_org, test_user):
    at = datetime(2020, 2, 29, 23, 59, tzinfo=timezone.utc)
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test", created_at=at)
    db.add(conv)
    await db.flush()
    msg = Message(
        org_id=test_org.id, conversation_id=conv.id, role="user", content="Leap day", created_at=at
    )
    db.add(msg)
    await db.flush()
    before = (
        await db.execute(
            text("SELECT tableoid::regclass::text FROM conversations WHERE id=:id"), {"id": conv.id}
        )
    ).scalar_one()
    assert before == "conversations_default"
    from src.services.data_partitions import maintain_partitions

    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    moved = await maintain_partitions(db)
    await db.execute(text("RESET ROLE"))
    assert moved >= 2
    assert (
        await db.execute(text("SELECT content FROM messages WHERE id=:id"), {"id": msg.id})
    ).scalar_one() == "Leap day"
    assert (
        await db.execute(
            text("SELECT tableoid::regclass::text FROM conversations WHERE id=:id"), {"id": conv.id}
        )
    ).scalar_one() == "conversations_p20200201"


@pytest.mark.asyncio
async def test_app_direct_child_rls_and_registry_tombstone(db, test_org, test_user):
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    child = (
        await db.execute(
            text("SELECT tableoid::regclass::text FROM conversations WHERE id=:id"), {"id": conv.id}
        )
    ).scalar_one()
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    await db.execute(
        text("SELECT set_config('app.current_org_id',:org,true)"), {"org": str(uuid.uuid4())}
    )
    assert (
        await db.execute(text(f"SELECT count(*) FROM {child} WHERE id=:id"), {"id": conv.id})
    ).scalar_one() == 0
    await db.execute(
        text("SELECT set_config('app.current_org_id',:org,true)"), {"org": str(test_org.id)}
    )
    assert (
        await db.execute(text(f"SELECT count(*) FROM {child} WHERE id=:id"), {"id": conv.id})
    ).scalar_one() == 1
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM conversation_identities WHERE id=:id"), {"id": conv.id}
            )
    await db.execute(text("UPDATE conversations SET title='allowed' WHERE id=:id"), {"id": conv.id})
    await db.execute(text("RESET ROLE"))


@pytest.mark.asyncio
async def test_default_capacity_is_global_across_tenants(db, test_org):
    # Seed capacity without quadratic per-row admission, then exercise the real
    # application trigger as a different tenant. This is an operator test fixture.
    await db.execute(text("ALTER TABLE audit_logs_default DISABLE TRIGGER bounded_default"))
    await db.execute(
        text("""INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at)
        SELECT gen_random_uuid(),:org,'POST','/fixture',200,'1901-01-01'::timestamptz
        FROM generate_series(1,10000)"""),
        {"org": test_org.id},
    )
    await db.execute(text("ALTER TABLE audit_logs_default ENABLE TRIGGER bounded_default"))
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    other = uuid.uuid4()
    await db.execute(text("SELECT set_config('app.current_org_id',:org,true)"), {"org": str(other)})
    with pytest.raises(DBAPIError, match="capacity"):
        async with db.begin_nested():
            await db.execute(
                text("""INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at)
                VALUES(gen_random_uuid(),:org,'POST','/fixture',200,'1901-01-01')"""),
                {"org": other},
            )
    await db.execute(text("RESET ROLE"))


@pytest.mark.asyncio
async def test_direct_child_insert_and_copy_cannot_forge_message_identity(db, test_org, test_user):
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    msg = Message(org_id=test_org.id, conversation_id=conv.id, turn_id=uuid.uuid4(), role="user")
    db.add(msg)
    await db.flush()
    child = (
        await db.execute(
            text("SELECT tableoid::regclass::text FROM messages WHERE id=:id"), {"id": msg.id}
        )
    ).scalar_one()
    for statement, params in [
        (
            f"INSERT INTO {child}(id,org_id,conversation_id,turn_id,role,created_at) VALUES(:id,:org,:conv,:turn,'user',:at)",
            dict(
                id=uuid.uuid4(), org=test_org.id, conv=conv.id, turn=msg.turn_id, at=msg.created_at
            ),
        ),
        (f"UPDATE {child} SET turn_id=:turn WHERE id=:id", dict(id=msg.id, turn=uuid.uuid4())),
        (f"UPDATE {child} SET org_id=:org WHERE id=:id", dict(id=msg.id, org=uuid.uuid4())),
    ]:
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(text(statement), params)
    # COPY uses the same database admission guards as INSERT, including direct children.
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    with pytest.raises(Exception, match="unique"):
        async with db.begin_nested():
            await raw.driver_connection.copy_records_to_table(
                child,
                records=[(uuid.uuid4(), test_org.id, conv.id, msg.turn_id, "user", msg.created_at)],
                columns=["id", "org_id", "conversation_id", "turn_id", "role", "created_at"],
            )


@pytest.mark.asyncio
async def test_global_audit_uuid_and_archived_registry_reject_resurrection(db, test_org):
    identifier = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES(:id,:org,'POST','/fixture',200,'2024-01-31')"
        ),
        dict(id=identifier, org=test_org.id),
    )
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES(:id,:org,'POST','/fixture',200,'2024-02-01')"
                ),
                dict(id=identifier, org=test_org.id),
            )
    await db.execute(
        text("UPDATE audit_log_identities SET lifecycle='archived' WHERE id=:id"),
        {"id": identifier},
    )
    await db.execute(text("DELETE FROM audit_logs WHERE id=:id"), {"id": identifier})
    with pytest.raises(DBAPIError, match="unavailable"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES(:id,:org,'POST','/fixture',200,'2024-01-31')"
                ),
                dict(id=identifier, org=test_org.id),
            )


@pytest.mark.asyncio
async def test_payload_delete_cascades_graph_but_retains_identities(db, test_org, test_user):
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    msg = Message(org_id=test_org.id, conversation_id=conv.id, role="user")
    db.add(msg)
    await db.flush()
    await db.execute(text("DELETE FROM conversations WHERE id=:id"), {"id": conv.id})
    assert (
        await db.execute(text("SELECT count(*) FROM messages WHERE id=:id"), {"id": msg.id})
    ).scalar_one() == 0
    assert (
        await db.execute(
            text("SELECT count(*) FROM message_identities WHERE id=:id"), {"id": msg.id}
        )
    ).scalar_one() == 1
    assert (
        await db.execute(
            text("SELECT count(*) FROM conversation_identities WHERE id=:id"), {"id": conv.id}
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_partition_maintenance_rollback_keeps_default_payload(db, test_org, test_user):
    conv = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        model="test",
        created_at=datetime(2021, 3, 1, tzinfo=timezone.utc),
    )
    db.add(conv)
    await db.flush()
    from src.services.data_partitions import maintain_partitions

    with pytest.raises(RuntimeError, match="injected"):
        async with db.begin_nested():
            await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
            await maintain_partitions(db)
            await db.execute(text("RESET ROLE"))
            assert (
                await db.execute(text("SELECT to_regclass('public.conversations_p20210301')"))
            ).scalar_one() is not None
            raise RuntimeError("injected pre-commit failure")
    assert (
        await db.execute(text("SELECT to_regclass('public.conversations_p20210301')"))
    ).scalar_one() is None
    assert (
        await db.execute(
            text("SELECT tableoid::regclass::text FROM conversations WHERE id=:id"), {"id": conv.id}
        )
    ).scalar_one() == "conversations_default"


@pytest.mark.asyncio
async def test_maintenance_denies_unvalidated_relations_and_superuser_service(db):
    from src.services.data_partitions import maintain_partitions

    with pytest.raises(ValueError, match="restricted"):
        await maintain_partitions(db)
    with pytest.raises(DBAPIError, match="Invalid partition target"):
        async with db.begin_nested():
            await db.execute(text("SELECT platform_ensure_partition('users',now())"))
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    with pytest.raises(DBAPIError, match="permission denied"):
        async with db.begin_nested():
            await db.execute(text("SELECT platform_ensure_partition('messages',now())"))
    await db.execute(text("RESET ROLE"))
