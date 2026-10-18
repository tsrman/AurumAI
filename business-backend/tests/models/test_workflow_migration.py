import os
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings


WORKFLOW_TABLES = (
    "workflow_definitions",
    "workflow_nodes",
    "workflow_edges",
    "workflow_runs",
    "workflow_node_runs",
)


def _runtime_db_url() -> str:
    parsed = urlparse(settings.DATABASE_URL)
    password = os.environ.get("APP_DATABASE_ROLE_PASSWORD", "aurum_ai_app")
    return urlunparse(
        parsed._replace(
            netloc=f"aurum_ai_app:{password}@{parsed.hostname}:{parsed.port or 5432}"
        )
    )


async def test_workflow_migration_enforces_rls_runtime_dml_and_contracts(db) -> None:
    rls_rows = (
        await db.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(:tables)"
            ),
            {"tables": list(WORKFLOW_TABLES)},
        )
    ).all()
    policies = (
        await db.execute(
            text(
                "SELECT tablename, qual, with_check FROM pg_policies "
                "WHERE tablename = ANY(:tables)"
            ),
            {"tables": list(WORKFLOW_TABLES)},
        )
    ).all()
    privilege_rows = (
        await db.execute(
            text(
                "SELECT table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE grantee = 'aurum_ai_app' AND table_name = ANY(:tables)"
            ),
            {"tables": list(WORKFLOW_TABLES)},
        )
    ).all()
    constraints = {
        name: definition
        for name, definition in (
            await db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = ANY(:names)"
                ),
                {
                    "names": [
                        "ck_access_grants_resource_type",
                        "fk_workflow_definitions_org_supervisor",
                        "fk_workflow_nodes_org_definition",
                        "fk_workflow_nodes_org_agent",
                        "fk_workflow_edges_source_node",
                        "fk_workflow_edges_target_node",
                        "fk_workflow_runs_org_definition",
                        "fk_workflow_runs_org_user",
                        "fk_workflow_runs_org_conversation",
                        "fk_workflow_runs_org_message",
                        "fk_workflow_node_runs_org_run",
                    ]
                },
            )
        ).all()
    }
    correlation_columns = (
        await db.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'llm_usage_events' "
                "AND column_name = ANY(:columns)"
            ),
            {
                "columns": [
                    "workflow_run_id",
                    "workflow_phase",
                    "workflow_node_key",
                ]
            },
        )
    ).all()
    status_defaults = dict(
        (
            await db.execute(
                text(
                    "SELECT table_name, column_default FROM information_schema.columns "
                    "WHERE column_name = 'status' AND table_name = ANY(:tables)"
                ),
                {
                    "tables": [
                        "workflow_definitions",
                        "workflow_runs",
                        "workflow_node_runs",
                    ]
                },
            )
        ).all()
    )
    message_model_length = await db.scalar(
        text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'messages' AND column_name = 'model'"
        )
    )

    assert {name: (rls, forced) for name, rls, forced in rls_rows} == {
        name: (True, True) for name in WORKFLOW_TABLES
    }
    assert {name for name, _, _ in policies} == set(WORKFLOW_TABLES)
    assert all("app.current_org_id" in qual for _, qual, _ in policies)
    assert all("app.current_org_id" in check for _, _, check in policies)
    assert {
        table: {privilege for row_table, privilege in privilege_rows if row_table == table}
        for table in WORKFLOW_TABLES
    } == {
        table: {"SELECT", "INSERT", "UPDATE", "DELETE"} for table in WORKFLOW_TABLES
    }
    assert "'workflow'" in constraints["ck_access_grants_resource_type"]
    assert set(constraints) >= {
        "fk_workflow_definitions_org_supervisor",
        "fk_workflow_nodes_org_definition",
        "fk_workflow_nodes_org_agent",
        "fk_workflow_edges_source_node",
        "fk_workflow_edges_target_node",
        "fk_workflow_runs_org_definition",
        "fk_workflow_runs_org_user",
        "fk_workflow_runs_org_conversation",
        "fk_workflow_runs_org_message",
        "fk_workflow_node_runs_org_run",
    }
    assert dict(correlation_columns) == {
        "workflow_node_key": "YES",
        "workflow_phase": "YES",
        "workflow_run_id": "YES",
    }
    assert status_defaults == {
        "workflow_definitions": "'active'::character varying",
        "workflow_runs": "'pending'::character varying",
        "workflow_node_runs": "'pending'::character varying",
    }
    assert message_model_length == 255


async def test_same_org_foreign_keys_reject_cross_org_supervisor() -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    agent_b = uuid.uuid4()
    definition = uuid.uuid4()
    admin_engine = create_async_engine(settings.DATABASE_URL)
    sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) "
                    "VALUES (:id, :name, :slug)"
                ),
                [
                    {"id": org_a, "name": "Workflow FK A", "slug": f"wf-fk-a-{org_a}"},
                    {"id": org_b, "name": "Workflow FK B", "slug": f"wf-fk-b-{org_b}"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO agent_definitions "
                    "(id, org_id, name, model_id, system_prompt) "
                    "VALUES (:id, :org_id, 'Foreign supervisor', 'model', 'prompt')"
                ),
                {"id": agent_b, "org_id": org_b},
            )
            await session.commit()

        with pytest.raises(IntegrityError):
            async with sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO workflow_definitions "
                        "(id, org_id, name, supervisor_agent_definition_id) "
                        "VALUES (:id, :org_id, 'Invalid workflow', :agent_id)"
                    ),
                    {"id": definition, "org_id": org_a, "agent_id": agent_b},
                )
                await session.commit()
    finally:
        async with sessions() as session:
            await session.execute(
                text("DELETE FROM workflow_definitions WHERE id = :id"),
                {"id": definition},
            )
            await session.execute(
                text("DELETE FROM agent_definitions WHERE id = :id"),
                {"id": agent_b},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.commit()
        await admin_engine.dispose()


async def test_runtime_role_hides_and_rejects_cross_org_workflow_rows() -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    workflow_a = uuid.uuid4()
    workflow_b = uuid.uuid4()
    forged = uuid.uuid4()
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
                    {"id": org_a, "name": "Workflow RLS A", "slug": f"wf-a-{org_a}"},
                    {"id": org_b, "name": "Workflow RLS B", "slug": f"wf-b-{org_b}"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO agent_definitions "
                    "(id, org_id, name, model_id, system_prompt) "
                    "VALUES (:id, :org_id, :name, 'model', 'prompt')"
                ),
                [
                    {"id": agent_a, "org_id": org_a, "name": "Supervisor A"},
                    {"id": agent_b, "org_id": org_b, "name": "Supervisor B"},
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO workflow_definitions "
                    "(id, org_id, name, supervisor_agent_definition_id) "
                    "VALUES (:id, :org_id, :name, :agent_id)"
                ),
                [
                    {
                        "id": workflow_a,
                        "org_id": org_a,
                        "name": "Workflow A",
                        "agent_id": agent_a,
                    },
                    {
                        "id": workflow_b,
                        "org_id": org_b,
                        "name": "Workflow B",
                        "agent_id": agent_b,
                    },
                ],
            )
            await session.commit()

        async with app_sessions() as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            visible = (
                await session.execute(text("SELECT id FROM workflow_definitions"))
            ).scalars().all()
            assert visible == [workflow_a]

        with pytest.raises(ProgrammingError):
            async with app_sessions() as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO workflow_definitions "
                        "(id, org_id, name, supervisor_agent_definition_id) "
                        "VALUES (:id, :org_id, 'forged', :agent_id)"
                    ),
                    {"id": forged, "org_id": org_b, "agent_id": agent_b},
                )
                await session.commit()
    finally:
        await app_engine.dispose()
        async with admin_sessions() as session:
            await session.execute(
                text("DELETE FROM workflow_definitions WHERE id IN (:a, :b, :forged)"),
                {"a": workflow_a, "b": workflow_b, "forged": forged},
            )
            await session.execute(
                text("DELETE FROM agent_definitions WHERE id IN (:a, :b)"),
                {"a": agent_a, "b": agent_b},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.commit()
        await admin_engine.dispose()
