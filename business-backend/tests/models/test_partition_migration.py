"""Maintenance-window upgrade/downgrade on a private, populated database."""

import asyncio
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from src.core.config import settings


@pytest.mark.asyncio
async def test_populated_partition_roundtrip_and_restore_preflight():
    url = make_url(settings.DATABASE_URL)
    name = "partition_roundtrip_" + uuid.uuid4().hex
    admin_url = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    test_url = url.set(database=name).render_as_string(hide_password=False)
    admin = await asyncpg.connect(admin_url)
    conn = None

    async def migrate(action, revision, *, success=True):
        env = {**os.environ, "APP_DATABASE_URL": test_url}
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            action,
            revision,
            cwd=Path(__file__).parents[2],
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert (proc.returncode == 0) == success, (stdout + stderr).decode()
        return (stdout + stderr).decode()

    try:
        await admin.execute(f"CREATE DATABASE {name}")
        conn = await asyncpg.connect(
            url.set(drivername="postgresql", database=name).render_as_string(hide_password=False)
        )
        await conn.execute("CREATE SCHEMA partman; CREATE EXTENSION pg_partman SCHEMA partman")
        await migrate("upgrade", "20260913_identity_sync")
        # Fixed literal IDs, non-rounded numerics, JSON, UTC boundaries and leap day.
        await conn.execute("""
            INSERT INTO organizations(id,name,slug) VALUES
              ('00000000-0000-0000-0000-000000000001','Partition A','partition-a'),
              ('00000000-0000-0000-0000-000000000002','Partition B','partition-b');
            INSERT INTO users(id,org_id,keycloak_id,email) VALUES
              ('00000000-0000-0000-0000-000000000011','00000000-0000-0000-0000-000000000001',gen_random_uuid(),'a@partition.test'),
              ('00000000-0000-0000-0000-000000000012','00000000-0000-0000-0000-000000000002',gen_random_uuid(),'b@partition.test');
            INSERT INTO conversations(id,org_id,user_id,model,created_at,cost_usd,extra) VALUES
              ('00000000-0000-0000-0000-000000000021','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000011','test','2024-01-31T23:59:59.999999Z',1.234567,'{"original":[1,null,"x"]}'),
              ('00000000-0000-0000-0000-000000000022','00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000012','test','2024-02-01T00:00:00Z',2.345678,'{}');
            INSERT INTO messages(id,org_id,conversation_id,turn_id,role,content,created_at,cost_usd) VALUES
              ('00000000-0000-0000-0000-000000000031','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000021','00000000-0000-0000-0000-000000000041','user','Question UTF-8: café ü','2024-01-31T23:59:59.999999Z',0.000001),
              ('00000000-0000-0000-0000-000000000032','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000021','00000000-0000-0000-0000-000000000041','assistant','Original answer','2024-02-01T00:00:00Z',0.000002),
              ('00000000-0000-0000-0000-000000000033','00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000022',NULL,'user','Leap','2024-02-29T23:59:59.999999Z',0);
            INSERT INTO audit_logs(id,org_id,method,path,status_code,created_at) VALUES
              ('00000000-0000-0000-0000-000000000051','00000000-0000-0000-0000-000000000001','POST','/fixture',201,'2024-02-29T23:59:59.999999Z');
            INSERT INTO agent_definitions(id,org_id,name,model_id,system_prompt) VALUES
              ('00000000-0000-0000-0000-000000000061','00000000-0000-0000-0000-000000000001','Agent','test','Original prompt');
            INSERT INTO workflow_definitions(id,org_id,name,supervisor_agent_definition_id) VALUES
              ('00000000-0000-0000-0000-000000000071','00000000-0000-0000-0000-000000000001','Definition','00000000-0000-0000-0000-000000000061');
            INSERT INTO workflow_runs(id,org_id,workflow_definition_id,workflow_definition_version,initiating_user_id,conversation_id,turn_id,source_message_id,temporal_workflow_id,execution_snapshot,status) VALUES
              ('00000000-0000-0000-0000-000000000081','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000071',1,'00000000-0000-0000-0000-000000000011','00000000-0000-0000-0000-000000000021','00000000-0000-0000-0000-000000000041','00000000-0000-0000-0000-000000000031','partition-owned-workflow','{"original":true}','completed');
            INSERT INTO llm_usage_events(id,org_id,user_id,request_id,period_start,model,workflow_run_id,workflow_phase,prompt_tokens,total_tokens) VALUES
              ('00000000-0000-0000-0000-000000000091','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000011',gen_random_uuid(),'2024-02-01','test','00000000-0000-0000-0000-000000000081','supervisor_synthesize',17,17);
        """)
        tables = ("conversations", "messages", "audit_logs", "workflow_runs", "llm_usage_events")

        async def snapshot():
            return {
                table: await conn.fetchval(
                    f"SELECT jsonb_agg(to_jsonb(t) ORDER BY id)::text FROM {table} t"
                )
                for table in tables
            }

        original = await snapshot()
        await migrate("upgrade", "20260913_partitions")
        assert await snapshot() == original
        # Session-local objects must not shadow catalog reads in definer DDL.
        shadow = conn.transaction()
        await shadow.start()
        await conn.execute(
            "CREATE TEMP TABLE pg_attribute(attname name,attnum smallint,attrelid oid,attisdropped boolean)"
        )
        assert (
            await conn.fetchval("SELECT platform_ensure_partition('audit_logs','1904-01-01')") == 0
        )
        await shadow.rollback()
        # The new retained identity owner FK checks at commit, after nested org
        # cascades. Runtime owner and billing checks remain immediate RESTRICT.
        transaction = conn.transaction()
        await transaction.start()
        await conn.execute("""
            INSERT INTO users(id,org_id,keycloak_id,email) VALUES
              ('00000000-0000-0000-0000-000000000013','00000000-0000-0000-0000-000000000001',gen_random_uuid(),'surviving@partition.test');
            INSERT INTO workflow_runs(id,org_id,workflow_definition_id,workflow_definition_version,initiating_user_id,conversation_id,turn_id,source_message_id,temporal_workflow_id,execution_snapshot,status)
            SELECT '00000000-0000-0000-0000-000000000082',org_id,workflow_definition_id,workflow_definition_version,
              '00000000-0000-0000-0000-000000000013',conversation_id,gen_random_uuid(),source_message_id,
              'partition-owned-other-workflow',execution_snapshot,status FROM workflow_runs;
            DELETE FROM workflow_runs WHERE id='00000000-0000-0000-0000-000000000082';
        """)
        await conn.execute("DELETE FROM users WHERE id='00000000-0000-0000-0000-000000000013'")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await transaction.commit()
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM workflow_run_identities WHERE id='00000000-0000-0000-0000-000000000082'"
            )
            == 0
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM workflow_run_identities WHERE id='00000000-0000-0000-0000-000000000081'"
                )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM workflow_runs WHERE id='00000000-0000-0000-0000-000000000081'"
                )
        transaction = conn.transaction()
        await transaction.start()
        await conn.execute(
            "DELETE FROM llm_usage_events; DELETE FROM workflow_runs; DELETE FROM workflow_definitions; DELETE FROM agent_definitions; DELETE FROM organizations"
        )
        assert await conn.fetchval("SELECT count(*) FROM workflow_run_identities") == 0
        assert await conn.fetchval("SELECT count(*) FROM conversation_identities") == 0
        await transaction.rollback()
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM pg_partitioned_table WHERE partrelid IN ('conversations'::regclass,'messages'::regclass,'audit_logs'::regclass)"
            )
            == 3
        )
        plan = await conn.fetchval(
            "EXPLAIN (FORMAT JSON) SELECT * FROM messages WHERE id='00000000-0000-0000-0000-000000000031' AND created_at='2024-01-31T23:59:59.999999Z'"
        )
        assert (
            "messages_p20240101" in plan
            and "messages_p20240201" not in plan
            and "messages_default" not in plan
        )
        # Ordinary hard-delete has no archive to restore. Its unreferenced active
        # tombstones must not make rollback to the original schema impossible.
        await conn.execute("""
            INSERT INTO conversations(id,org_id,user_id,model) VALUES
              ('00000000-0000-0000-0000-000000000023','00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000012','test');
            INSERT INTO messages(id,org_id,conversation_id,role,content) VALUES
              ('00000000-0000-0000-0000-000000000034','00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000023','user','ordinary delete');
            DELETE FROM conversations WHERE id='00000000-0000-0000-0000-000000000023';
        """)
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM conversation_identities WHERE id='00000000-0000-0000-0000-000000000023'"
            )
            == 1
        )
        await migrate("downgrade", "20260913_identity_sync")
        assert await snapshot() == original
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM pg_partitioned_table WHERE partrelid IN ('conversations'::regclass,'messages'::regclass,'audit_logs'::regclass)"
            )
            == 0
        )
        await migrate("upgrade", "20260913_partitions")
        # Simulate separately authorized archive purge only in this owned fixture.
        await conn.execute(
            "UPDATE conversation_identities SET lifecycle='archived' WHERE id='00000000-0000-0000-0000-000000000021'"
        )
        await conn.execute(
            "DELETE FROM workflow_runs WHERE id='00000000-0000-0000-0000-000000000081'"
        )
        assert await conn.fetchval("SELECT total_tokens FROM llm_usage_events") == 17
        failure = await migrate("downgrade", "20260913_identity_sync", success=False)
        assert "Restore missing archived payloads" in failure
        assert (
            await conn.fetchval("SELECT version_num FROM alembic_version") == "20260913_partitions"
        )
        assert await conn.fetchval("SELECT count(*) FROM workflow_run_identities") == 1
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM llm_usage_events; DELETE FROM workflow_runs; DELETE FROM workflow_definitions; DELETE FROM agent_definitions; DELETE FROM organizations"
            )
        assert await conn.fetchval("SELECT count(*) FROM workflow_run_identities") == 0
        assert await conn.fetchval("SELECT count(*) FROM conversation_identities") == 0
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f"DROP DATABASE IF EXISTS {name}")
        await admin.close()
