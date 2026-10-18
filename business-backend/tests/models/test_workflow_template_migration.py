import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import settings


def _runtime_db_url() -> str:
    parsed = urlparse(settings.DATABASE_URL)
    password = os.environ.get("APP_DATABASE_ROLE_PASSWORD", "aurum_ai_app")
    return urlunparse(
        parsed._replace(
            netloc=f"aurum_ai_app:{password}@{parsed.hostname}:{parsed.port or 5432}"
        )
    )


@asynccontextmanager
async def _session_for(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            yield session
    finally:
        await engine.dispose()


async def _insert_orgs(session: AsyncSession, org_a: uuid.UUID, org_b: uuid.UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (:id, :name, :slug)"
        ),
        [
            {"id": org_a, "name": "Template test A", "slug": f"template-a-{org_a}"},
            {"id": org_b, "name": "Template test B", "slug": f"template-b-{org_b}"},
        ],
    )


def test_workflow_template_model_exposes_storage_fields() -> None:
    from src.models.workflow_template import WorkflowTemplate

    assert set(WorkflowTemplate.__table__.columns.keys()) == {
        "id",
        "org_id",
        "name",
        "description",
        "status",
        "version",
        "snapshot",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    }


async def test_template_table_has_force_rls(db) -> None:
    row = (
        await db.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'workflow_templates'"
            )
        )
    ).one_or_none()

    assert row is not None
    assert tuple(row) == (True, True)


async def test_template_table_has_policy_privileges_and_storage_contract(db) -> None:
    policy = (
        await db.execute(
            text(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE tablename = 'workflow_templates' "
                "AND policyname = 'workflow_templates_org_isolation'"
            )
        )
    ).one()
    privileges = (
        await db.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'aurum_ai_app' "
                "AND table_name = 'workflow_templates'"
            )
        )
    ).scalars().all()
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
                        "ck_workflow_templates_name_nonblank",
                        "ck_workflow_templates_status",
                        "ck_workflow_templates_version",
                        "ck_workflow_templates_snapshot_object",
                        "uq_workflow_templates_org_name",
                        "uq_workflow_templates_org_id_id",
                        "fk_workflow_definitions_org_source_template",
                        "ck_workflow_definitions_source_template_provenance",
                    ]
                },
            )
        ).all()
    }
    columns = {
        name: (nullable, default)
        for name, nullable, default in (
            await db.execute(
                text(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'workflow_templates'"
                )
            )
        ).all()
    }
    foreign_keys = {
        name: definition
        for name, definition in (
            await db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'workflow_templates'::regclass AND contype = 'f'"
                )
            )
        ).all()
    }
    indexes = set(
        (
            await db.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'workflow_templates'"
                )
            )
        ).scalars()
    )

    assert "app.current_org_id" in policy.qual
    assert "app.current_org_id" in policy.with_check
    assert set(privileges) == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert set(constraints) == {
        "ck_workflow_templates_name_nonblank",
        "ck_workflow_templates_status",
        "ck_workflow_templates_version",
        "ck_workflow_templates_snapshot_object",
        "uq_workflow_templates_org_name",
        "uq_workflow_templates_org_id_id",
        "fk_workflow_definitions_org_source_template",
        "ck_workflow_definitions_source_template_provenance",
    }
    assert "ON DELETE RESTRICT" in constraints[
        "fk_workflow_definitions_org_source_template"
    ]
    assert columns["description"][0] == "YES"
    assert columns["created_by"][0] == "YES"
    assert columns["updated_by"][0] == "YES"
    assert columns["status"] == ("NO", "'active'::character varying")
    assert columns["version"] == ("NO", "1")
    assert columns["snapshot"] == ("NO", None)
    assert columns["org_id"][0] == "NO"
    assert "organizations(id) ON DELETE CASCADE" in foreign_keys[
        "workflow_templates_org_id_fkey"
    ]
    assert "users(id) ON DELETE SET NULL" in foreign_keys[
        "workflow_templates_created_by_fkey"
    ]
    assert "users(id) ON DELETE SET NULL" in foreign_keys[
        "workflow_templates_updated_by_fkey"
    ]
    assert "ix_workflow_templates_org_status_name" in indexes


@pytest.mark.parametrize(
    ("overrides", "expected_constraint"),
    [
        ({"name": "   "}, "ck_workflow_templates_name_nonblank"),
        ({"status": "deleted"}, "ck_workflow_templates_status"),
        ({"version": 0}, "ck_workflow_templates_version"),
        ({"snapshot": "[]"}, "ck_workflow_templates_snapshot_object"),
    ],
)
async def test_template_checks_reject_invalid_rows(
    overrides: dict[str, object], expected_constraint: str
) -> None:
    org_id = uuid.uuid4()
    template_id = uuid.uuid4()
    values: dict[str, object] = {
        "id": template_id,
        "org_id": org_id,
        "name": "Valid template",
        "status": "active",
        "version": 1,
        "snapshot": "{}",
    }
    values.update(overrides)

    async with _session_for(settings.DATABASE_URL) as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (:id, 'Template checks', :slug)"
            ),
            {"id": org_id, "slug": f"template-checks-{org_id}"},
        )
        await session.commit()

    try:
        with pytest.raises(IntegrityError) as exc_info:
            async with _session_for(_runtime_db_url()) as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_id)},
                )
                await session.execute(
                    text(
                        "INSERT INTO workflow_templates "
                        "(id, org_id, name, status, version, snapshot) "
                        "VALUES (:id, :org_id, :name, :status, :version, "
                        "CAST(:snapshot AS jsonb))"
                    ),
                    values,
                )
                await session.commit()
        assert expected_constraint in str(exc_info.value.orig)
    finally:
        async with _session_for(settings.DATABASE_URL) as session:
            await session.execute(
                text("DELETE FROM workflow_templates WHERE id = :id"),
                {"id": template_id},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )
            await session.commit()


async def test_template_name_is_unique_within_organization() -> None:
    org_id = uuid.uuid4()
    template_a = uuid.uuid4()
    template_b = uuid.uuid4()
    async with _session_for(settings.DATABASE_URL) as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (:id, 'Template unique', :slug)"
            ),
            {"id": org_id, "slug": f"template-unique-{org_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_templates (id, org_id, name, snapshot) "
                "VALUES (:id, :org_id, 'Duplicate', CAST('{}' AS jsonb))"
            ),
            {"id": template_a, "org_id": org_id},
        )
        await session.commit()

    try:
        with pytest.raises(IntegrityError) as exc_info:
            async with _session_for(_runtime_db_url()) as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_id)},
                )
                await session.execute(
                    text(
                        "INSERT INTO workflow_templates (id, org_id, name, snapshot) "
                        "VALUES (:id, :org_id, 'Duplicate', CAST('{}' AS jsonb))"
                    ),
                    {"id": template_b, "org_id": org_id},
                )
                await session.commit()
        assert "uq_workflow_templates_org_name" in str(exc_info.value.orig)
    finally:
        async with _session_for(settings.DATABASE_URL) as session:
            await session.execute(
                text("DELETE FROM workflow_templates WHERE org_id = :org_id"),
                {"org_id": org_id},
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )
            await session.commit()


async def test_runtime_role_enforces_template_rls_and_context_switching() -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    template_a = uuid.uuid4()
    template_b = uuid.uuid4()
    forged = uuid.uuid4()

    async with _session_for(settings.DATABASE_URL) as session:
        await _insert_orgs(session, org_a, org_b)
        await session.execute(
            text(
                "INSERT INTO workflow_templates (id, org_id, name, snapshot) "
                "VALUES (:id, :org_id, 'Foreign B', CAST('{}' AS jsonb))"
            ),
            {"id": template_b, "org_id": org_b},
        )
        await session.commit()

    try:
        async with _session_for(_runtime_db_url()) as session:
            assert (
                await session.execute(text("SELECT id FROM workflow_templates"))
            ).scalars().all() == []

        with pytest.raises(ProgrammingError):
            async with _session_for(_runtime_db_url()) as session:
                await session.execute(
                    text(
                        "INSERT INTO workflow_templates (id, org_id, name, snapshot) "
                        "VALUES (:id, :org_id, 'No context', CAST('{}' AS jsonb))"
                    ),
                    {"id": forged, "org_id": org_a},
                )
                await session.commit()

        async with _session_for(_runtime_db_url()) as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            await session.execute(
                text(
                    "INSERT INTO workflow_templates (id, org_id, name, snapshot) "
                    "VALUES (:id, :org_id, 'Own A', CAST('{}' AS jsonb))"
                ),
                {"id": template_a, "org_id": org_a},
            )
            await session.commit()

        async with _session_for(_runtime_db_url()) as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            visible = (
                await session.execute(
                    text("SELECT id FROM workflow_templates ORDER BY name")
                )
            ).scalars().all()
            assert visible == [template_a]
            updated = await session.execute(
                text(
                    "UPDATE workflow_templates SET description = 'updated' "
                    "WHERE id = :id RETURNING description"
                ),
                {"id": template_a},
            )
            assert updated.scalar_one() == "updated"
            await session.commit()

        with pytest.raises(ProgrammingError):
            async with _session_for(_runtime_db_url()) as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO workflow_templates "
                        "(id, org_id, name, snapshot) "
                        "VALUES (:id, :org_id, 'Foreign insert', CAST('{}' AS jsonb))"
                    ),
                    {"id": forged, "org_id": org_b},
                )
                await session.commit()

        with pytest.raises(ProgrammingError):
            async with _session_for(_runtime_db_url()) as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text("UPDATE workflow_templates SET org_id = :org_b WHERE id = :id"),
                    {"org_b": org_b, "id": template_a},
                )
                await session.commit()

        pinned_engine = create_async_engine(_runtime_db_url())
        try:
            async with pinned_engine.connect() as connection:
                backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))

                await connection.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                assert (
                    await connection.execute(text("SELECT id FROM workflow_templates"))
                ).scalars().all() == [template_a]

                await connection.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_b)},
                )
                assert await connection.scalar(text("SELECT pg_backend_pid()")) == backend_pid
                assert (
                    await connection.execute(text("SELECT id FROM workflow_templates"))
                ).scalars().all() == [template_b]

                await connection.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                assert await connection.scalar(text("SELECT pg_backend_pid()")) == backend_pid
                assert (
                    await connection.execute(text("SELECT id FROM workflow_templates"))
                ).scalars().all() == [template_a]
        finally:
            await pinned_engine.dispose()
    finally:
        async with _session_for(settings.DATABASE_URL) as session:
            await session.execute(
                text(
                    "DELETE FROM workflow_templates "
                    "WHERE id IN (:template_a, :template_b, :forged)"
                ),
                {
                    "template_a": template_a,
                    "template_b": template_b,
                    "forged": forged,
                },
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:org_a, :org_b)"),
                {"org_a": org_a, "org_b": org_b},
            )
            await session.commit()


@pytest.mark.parametrize(
    ("source_template_id", "source_template_version"),
    [("own", None), (None, 1), ("own", 0)],
)
async def test_workflow_provenance_requires_complete_positive_pair(
    source_template_id: str | None, source_template_version: int | None
) -> None:
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    template_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    resolved_template_id = template_id if source_template_id == "own" else None

    async with _session_for(settings.DATABASE_URL) as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (:id, 'Provenance pair', :slug)"
            ),
            {"id": org_id, "slug": f"provenance-pair-{org_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO agent_definitions "
                "(id, org_id, name, model_id, system_prompt) "
                "VALUES (:id, :org_id, 'Supervisor', 'model', 'prompt')"
            ),
            {"id": agent_id, "org_id": org_id},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_templates (id, org_id, name, snapshot) "
                "VALUES (:id, :org_id, 'Source', CAST('{}' AS jsonb))"
            ),
            {"id": template_id, "org_id": org_id},
        )
        await session.commit()

    try:
        with pytest.raises(IntegrityError) as exc_info:
            async with _session_for(_runtime_db_url()) as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_id)},
                )
                await session.execute(
                    text(
                        "INSERT INTO workflow_definitions "
                        "(id, org_id, name, supervisor_agent_definition_id, "
                        "source_template_id, source_template_version) "
                        "VALUES (:id, :org_id, 'Copy', :agent_id, "
                        ":template_id, :template_version)"
                    ),
                    {
                        "id": workflow_id,
                        "org_id": org_id,
                        "agent_id": agent_id,
                        "template_id": resolved_template_id,
                        "template_version": source_template_version,
                    },
                )
                await session.commit()
        assert "ck_workflow_definitions_source_template_provenance" in str(
            exc_info.value.orig
        )
    finally:
        async with _session_for(settings.DATABASE_URL) as session:
            await session.execute(
                text("DELETE FROM workflow_definitions WHERE id = :id"),
                {"id": workflow_id},
            )
            await session.execute(
                text("DELETE FROM workflow_templates WHERE id = :id"),
                {"id": template_id},
            )
            await session.execute(
                text("DELETE FROM agent_definitions WHERE id = :id"), {"id": agent_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )
            await session.commit()


async def test_workflow_provenance_is_same_org_and_old_rows_remain_valid() -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    agent_a = uuid.uuid4()
    template_a = uuid.uuid4()
    template_b = uuid.uuid4()
    old_workflow = uuid.uuid4()
    valid_copy = uuid.uuid4()
    foreign_copy = uuid.uuid4()

    async with _session_for(settings.DATABASE_URL) as session:
        await _insert_orgs(session, org_a, org_b)
        await session.execute(
            text(
                "INSERT INTO agent_definitions "
                "(id, org_id, name, model_id, system_prompt) "
                "VALUES (:id, :org_id, 'Supervisor A', 'model', 'prompt')"
            ),
            {"id": agent_a, "org_id": org_a},
        )
        await session.execute(
            text(
                "INSERT INTO workflow_templates (id, org_id, name, snapshot) "
                "VALUES (:id, :org_id, :name, CAST('{}' AS jsonb))"
            ),
            [
                {"id": template_a, "org_id": org_a, "name": "Source A"},
                {"id": template_b, "org_id": org_b, "name": "Source B"},
            ],
        )
        await session.execute(
            text(
                "INSERT INTO workflow_definitions "
                "(id, org_id, name, supervisor_agent_definition_id, "
                "source_template_id, source_template_version) "
                "VALUES (:id, :org_id, :name, :agent_id, :template_id, :version)"
            ),
            [
                {
                    "id": old_workflow,
                    "org_id": org_a,
                    "name": "Old workflow",
                    "agent_id": agent_a,
                    "template_id": None,
                    "version": None,
                },
                {
                    "id": valid_copy,
                    "org_id": org_a,
                    "name": "Valid copy",
                    "agent_id": agent_a,
                    "template_id": template_a,
                    "version": 1,
                },
            ],
        )
        await session.commit()

    try:
        async with _session_for(_runtime_db_url()) as session:
            await session.execute(
                text("SELECT set_config('app.current_org_id', :id, false)"),
                {"id": str(org_a)},
            )
            stored = (
                await session.execute(
                    text(
                        "SELECT id, source_template_id, source_template_version "
                        "FROM workflow_definitions "
                        "WHERE id IN (:old, :copy) ORDER BY name"
                    ),
                    {"old": old_workflow, "copy": valid_copy},
                )
            ).all()
            assert {row.id: (row.source_template_id, row.source_template_version) for row in stored} == {
                old_workflow: (None, None),
                valid_copy: (template_a, 1),
            }

        with pytest.raises(IntegrityError) as exc_info:
            async with _session_for(_runtime_db_url()) as session:
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :id, false)"),
                    {"id": str(org_a)},
                )
                await session.execute(
                    text(
                        "INSERT INTO workflow_definitions "
                        "(id, org_id, name, supervisor_agent_definition_id, "
                        "source_template_id, source_template_version) "
                        "VALUES (:id, :org_id, 'Foreign copy', :agent_id, "
                        ":template_id, 1)"
                    ),
                    {
                        "id": foreign_copy,
                        "org_id": org_a,
                        "agent_id": agent_a,
                        "template_id": template_b,
                    },
                )
                await session.commit()
        assert "fk_workflow_definitions_org_source_template" in str(
            exc_info.value.orig
        )
    finally:
        async with _session_for(settings.DATABASE_URL) as session:
            await session.execute(
                text(
                    "DELETE FROM workflow_definitions "
                    "WHERE id IN (:old, :valid, :foreign)"
                ),
                {"old": old_workflow, "valid": valid_copy, "foreign": foreign_copy},
            )
            await session.execute(
                text("DELETE FROM workflow_templates WHERE id IN (:a, :b)"),
                {"a": template_a, "b": template_b},
            )
            await session.execute(
                text("DELETE FROM agent_definitions WHERE id = :id"), {"id": agent_a}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
            await session.commit()
