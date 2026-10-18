from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models import conversation  # noqa: F401
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.organization import Organization
from src.models.user import User
from src.models.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from src.models.workflow_template import WorkflowTemplateStatus
from src.repositories.workflow_definition_repository import (
    WorkflowDefinitionRepository,
)
from src.repositories.workflow_template_repository import WorkflowTemplateRepository
from src.schemas.workflow_template import (
    TemplateCaptureRequest,
    TemplateInstantiateRequest,
    TemplateReplaceRequest,
    TemplateSnapshot,
    TemplateVersionRequest,
)
from src.services.model_catalog import GatewayCatalogSnapshot, GatewayCatalogStatus
from src.services.workflow_template import WorkflowTemplateService
from src.services.workflow_template_errors import WorkflowTemplateError
from src.services.workflow_template_snapshot import template_agent_name


async def available_catalog() -> GatewayCatalogSnapshot:
    return GatewayCatalogSnapshot(
        GatewayCatalogStatus.AVAILABLE,
        ({"id": "provider/model-v1"},),
    )


async def test_template_copy_preserves_inherited_temperature(
    db, test_org, test_user, template_snapshot
):
    template_snapshot.agents[0].temperature = None
    actor = _actor(test_org, test_user)
    template = await WorkflowTemplateRepository().create(
        db, actor, name="Inherited", description=None, snapshot=template_snapshot
    )
    await WorkflowTemplateService(catalog=available_catalog).instantiate(
        db, actor, template.id, TemplateInstantiateRequest(version=1, name="Copy")
    )
    agents = (await db.scalars(select(AgentDefinition).where(
        AgentDefinition.org_id == test_org.id
    ))).all()
    assert len(agents) == 1
    await db.refresh(agents[0])
    assert agents[0].temperature is None


def _actor(test_org: Organization, test_user: User) -> AuthorizationSubject:
    return AuthorizationSubject(test_user.id, test_org.id, "org_admin")


def _assert_error(
    caught: pytest.ExceptionInfo[WorkflowTemplateError],
    status_code: int,
    code: str,
) -> None:
    assert (caught.value.status_code, caught.value.code) == (status_code, code)


async def _org_row_counts(
    session: AsyncSession, org_id: UUID
) -> tuple[int, int]:
    agent_count = await session.scalar(
        select(func.count()).select_from(AgentDefinition).where(
            AgentDefinition.org_id == org_id
        )
    )
    workflow_count = await session.scalar(
        select(func.count()).select_from(WorkflowDefinition).where(
            WorkflowDefinition.org_id == org_id
        )
    )
    assert agent_count is not None
    assert workflow_count is not None
    return agent_count, workflow_count


async def test_two_copies_have_fresh_agents_saved_configuration_and_no_grants(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_snapshot: TemplateSnapshot,
) -> None:
    actor = _actor(test_org, test_user)
    template = await WorkflowTemplateRepository().create(
        db,
        actor,
        name="Saved",
        description="Copied description",
        snapshot=template_snapshot,
    )
    service = WorkflowTemplateService(catalog=available_catalog)

    first = await service.instantiate(
        db,
        actor,
        template.id,
        TemplateInstantiateRequest(version=1, name="First copy"),
    )
    second = await service.instantiate(
        db,
        actor,
        template.id,
        TemplateInstantiateRequest(version=1, name="Second copy"),
    )

    assert len(first.agent_ids) == len(second.agent_ids) == 1
    assert first.workflow.id != second.workflow.id
    assert first.agent_ids[0] != second.agent_ids[0]
    assert first.workflow.supervisor_agent_definition_id == first.agent_ids[0]
    assert second.workflow.supervisor_agent_definition_id == second.agent_ids[0]

    expected = template_snapshot.agents[0]
    first_agent = await db.get(AgentDefinition, first.agent_ids[0])
    second_agent = await db.get(AgentDefinition, second.agent_ids[0])
    assert first_agent is not None
    assert second_agent is not None
    for agent in (first_agent, second_agent):
        assert agent.name == template_agent_name(expected.name, agent.id)
        assert agent.description == expected.description
        assert agent.model_id == expected.model_id
        assert agent.system_prompt == expected.system_prompt
        assert agent.temperature == expected.temperature
        assert agent.skill_ids == expected.skill_ids
        assert agent.knowledge_base_ids == expected.knowledge_base_ids
        assert agent.created_by == test_user.id

    first_agent.system_prompt = "Only first copy changed"
    await db.flush()
    await db.refresh(second_agent)
    assert second_agent.system_prompt == "Answer"

    for copy in (first, second):
        assert copy.workflow.org_id == test_org.id
        assert copy.workflow.created_by == test_user.id
        assert copy.workflow.updated_by == test_user.id
        assert copy.workflow.description == "Copied description"
        assert copy.workflow.version == 1
        assert copy.workflow.status == WorkflowDefinitionStatus.ACTIVE.value
        assert copy.workflow.auto_routable is False
        assert copy.workflow.source_template_id == template.id
        assert copy.workflow.source_template_version == 1
        nodes, edges = await WorkflowDefinitionRepository().load_graph(
            db, test_org.id, copy.workflow.id
        )
        assert len(nodes) == 1
        assert nodes[0].agent_definition_id == copy.agent_ids[0]
        assert nodes[0].requires_confirmation is True
        assert edges == []

    created_ids = [
        str(first.workflow.id),
        str(second.workflow.id),
        *(str(value) for value in first.agent_ids),
        *(str(value) for value in second.agent_ids),
    ]
    grants = list(
        (
            await db.scalars(
                select(AccessGrant).where(AccessGrant.resource_id.in_(created_ids))
            )
        ).all()
    )
    assert grants == []


async def test_copies_survive_source_deletion_and_template_archive_unchanged(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    actor = _actor(test_org, test_user)
    service = WorkflowTemplateService(catalog=available_catalog)
    template = await service.capture(
        db,
        actor,
        TemplateCaptureRequest(
            source_workflow_id=template_source.id,
            source_workflow_version=1,
            name="Saved source",
            description="Snapshot description",
        ),
    )
    first = await service.instantiate(
        db,
        actor,
        template.id,
        TemplateInstantiateRequest(version=1, name="Before source deletion"),
    )
    first_agent = await db.get(AgentDefinition, first.agent_ids[0])
    source_agent = await db.get(
        AgentDefinition, template_source.supervisor_agent_definition_id
    )
    assert first_agent is not None
    assert source_agent is not None
    assert source_agent.system_prompt == "Answer"

    source_agent.system_prompt = "Edited after capture"
    await db.flush()
    template = await service.replace(
        db,
        actor,
        template.id,
        TemplateReplaceRequest(
            source_workflow_id=template_source.id,
            source_workflow_version=1,
            version=1,
            name="Updated saved source",
            description="Updated snapshot description",
        ),
    )
    assert first_agent.system_prompt == "Answer"
    await db.delete(template_source)
    await db.flush()
    await db.delete(source_agent)
    await db.flush()

    second = await service.instantiate(
        db,
        actor,
        template.id,
        TemplateInstantiateRequest(version=2, name="After source deletion"),
    )
    second_agent = await db.get(AgentDefinition, second.agent_ids[0])
    assert second_agent is not None
    assert second_agent.system_prompt == "Edited after capture"
    assert first_agent.system_prompt == "Answer"
    assert second.agent_ids != first.agent_ids

    archived = await service.archive(
        db,
        actor,
        template.id,
        TemplateVersionRequest(version=2),
    )

    assert archived.status == WorkflowTemplateStatus.ARCHIVED.value
    assert archived.version == 3
    await db.refresh(first.workflow)
    await db.refresh(second.workflow)
    assert first.workflow.status == WorkflowDefinitionStatus.ACTIVE.value
    assert second.workflow.status == WorkflowDefinitionStatus.ACTIVE.value
    assert first.workflow.version == second.workflow.version == 1
    assert first.workflow.source_template_id == template.id
    assert second.workflow.source_template_id == template.id
    assert first.workflow.source_template_version == 1
    assert second.workflow.source_template_version == 2

    with pytest.raises(WorkflowTemplateError) as caught:
        await service.instantiate(
            db,
            actor,
            template.id,
            TemplateInstantiateRequest(version=3, name="Archived copy"),
        )
    _assert_error(caught, 409, "template_archived")


async def test_instantiate_rejects_missing_stale_and_unavailable_dependencies_before_creation(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_snapshot: TemplateSnapshot,
) -> None:
    actor = _actor(test_org, test_user)
    service = WorkflowTemplateService(catalog=available_catalog)
    before = await _org_row_counts(db, test_org.id)

    with pytest.raises(WorkflowTemplateError) as missing:
        await service.instantiate(
            db,
            actor,
            uuid4(),
            TemplateInstantiateRequest(version=1, name="Missing"),
        )
    _assert_error(missing, 404, "template_not_found")

    template = await WorkflowTemplateRepository().create(
        db,
        actor,
        name="Dependency template",
        description=None,
        snapshot=template_snapshot,
    )
    after_template = await _org_row_counts(db, test_org.id)
    assert after_template == before

    with pytest.raises(WorkflowTemplateError) as stale:
        await service.instantiate(
            db,
            actor,
            template.id,
            TemplateInstantiateRequest(version=2, name="Stale"),
        )
    _assert_error(stale, 409, "version_conflict")

    async def empty_catalog() -> GatewayCatalogSnapshot:
        return GatewayCatalogSnapshot(GatewayCatalogStatus.AVAILABLE, ())

    with pytest.raises(WorkflowTemplateError) as unavailable:
        await WorkflowTemplateService(catalog=empty_catalog).instantiate(
            db,
            actor,
            template.id,
            TemplateInstantiateRequest(version=1, name="Unavailable"),
        )
    _assert_error(unavailable, 404, "dependency_unavailable")
    assert await _org_row_counts(db, test_org.id) == after_template


async def test_instantiate_revalidates_snapshot_against_current_limits_before_creation(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_snapshot: TemplateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _actor(test_org, test_user)
    template = await WorkflowTemplateRepository().create(
        db,
        actor,
        name="Limit template",
        description=None,
        snapshot=template_snapshot,
    )
    before = await _org_row_counts(db, test_org.id)
    monkeypatch.setattr(settings, "WORKFLOW_MAX_PARALLELISM", 1)

    with pytest.raises(WorkflowTemplateError) as over_limit:
        await WorkflowTemplateService(catalog=available_catalog).instantiate(
            db,
            actor,
            template.id,
            TemplateInstantiateRequest(version=1, name="Over current limit"),
        )

    _assert_error(over_limit, 422, "snapshot_invalid")
    assert await _org_row_counts(db, test_org.id) == before


async def test_archive_uses_current_version_and_is_idempotent(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_snapshot: TemplateSnapshot,
) -> None:
    actor = _actor(test_org, test_user)
    service = WorkflowTemplateService(catalog=available_catalog)
    template = await WorkflowTemplateRepository().create(
        db,
        actor,
        name="Archive template",
        description=None,
        snapshot=template_snapshot,
    )

    with pytest.raises(WorkflowTemplateError) as stale:
        await service.archive(
            db, actor, template.id, TemplateVersionRequest(version=2)
        )
    _assert_error(stale, 409, "version_conflict")
    assert template.status == WorkflowTemplateStatus.ACTIVE.value

    archived = await service.archive(
        db, actor, template.id, TemplateVersionRequest(version=1)
    )
    archived_again = await service.archive(
        db, actor, template.id, TemplateVersionRequest(version=2)
    )

    assert archived_again is archived
    assert archived_again.status == WorkflowTemplateStatus.ARCHIVED.value
    assert archived_again.version == 2

    with pytest.raises(WorkflowTemplateError) as missing:
        await service.archive(
            db, actor, uuid4(), TemplateVersionRequest(version=1)
        )
    _assert_error(missing, 404, "template_not_found")


async def test_instantiate_does_not_commit_and_caller_rollback_removes_all_created_rows(
    template_snapshot: TemplateSnapshot,
) -> None:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org_id = uuid4()
    user_id = uuid4()
    actor = AuthorizationSubject(user_id, org_id, "org_admin")
    template_id: UUID | None = None

    try:
        async with sessions() as seed:
            organization = Organization(
                id=org_id,
                name="Atomic instantiate owned fixture",
                slug=f"atomic-instantiate-{org_id}",
            )
            user = User(
                id=user_id,
                keycloak_id=uuid4(),
                email=f"atomic-instantiate-{org_id}@example.com",
                org_id=org_id,
                role="org_admin",
            )
            seed.add_all([organization, user])
            await seed.flush()
            template = await WorkflowTemplateRepository().create(
                seed,
                actor,
                name="Atomic template",
                description=None,
                snapshot=template_snapshot,
            )
            template_id = template.id
            await seed.commit()

        async with sessions() as caller, sessions() as observer:
            created = await WorkflowTemplateService(
                catalog=available_catalog
            ).instantiate(
                caller,
                actor,
                template_id,
                TemplateInstantiateRequest(version=1, name="Uncommitted copy"),
            )
            created_ids = [created.workflow.id, *created.agent_ids]

            assert await observer.scalar(
                select(func.count()).select_from(WorkflowDefinition).where(
                    WorkflowDefinition.id == created.workflow.id
                )
            ) == 0
            assert await observer.scalar(
                select(func.count()).select_from(AgentDefinition).where(
                    AgentDefinition.id.in_(created.agent_ids)
                )
            ) == 0

            await caller.rollback()

            assert await observer.scalar(
                select(func.count()).select_from(WorkflowDefinition).where(
                    WorkflowDefinition.id == created_ids[0]
                )
            ) == 0
            assert await observer.scalar(
                select(func.count()).select_from(AgentDefinition).where(
                    AgentDefinition.id.in_(created_ids[1:])
                )
            ) == 0
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(Organization).where(Organization.id == org_id)
            )
            await cleanup.commit()
        await engine.dispose()
