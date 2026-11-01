from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.models import conversation  # noqa: F401
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.knowledge_base import KnowledgeBase
from src.models.organization import Organization
from src.models.skill import Skill
from src.models.user import User
from src.models.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from src.models.workflow_template import WorkflowTemplate
from src.repositories.workflow_definition_repository import (
    WorkflowDefinitionRepository,
)
from src.schemas.workflow_template import (
    TemplateCaptureRequest,
    TemplateReplaceRequest,
    TemplateSnapshot,
)
from src.services.model_catalog import GatewayCatalogSnapshot, GatewayCatalogStatus
from src.services.workflow_template import WorkflowTemplateService
from src.services.workflow_template_errors import WorkflowTemplateError
from src.services.workflow_template_snapshot import workflow_draft_from_snapshot


async def available_catalog() -> GatewayCatalogSnapshot:
    return GatewayCatalogSnapshot(
        GatewayCatalogStatus.AVAILABLE,
        ({"id": "provider/model-v1"},),
    )


def _actor(test_org: Organization, test_user: User) -> AuthorizationSubject:
    return AuthorizationSubject(test_user.id, test_org.id, "org_admin")


def _capture_request(
    source: WorkflowDefinition,
    *,
    name: str = "Saved",
    source_version: int | None = None,
) -> TemplateCaptureRequest:
    return TemplateCaptureRequest(
        source_workflow_id=source.id,
        source_workflow_version=source_version or source.version,
        name=name,
        description=None,
    )


def _assert_error(
    caught: pytest.ExceptionInfo[WorkflowTemplateError],
    status_code: int,
    code: str,
) -> None:
    assert (caught.value.status_code, caught.value.code) == (status_code, code)


async def test_capture_preserves_source_configuration_hitl_and_deduplicates_agents(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    service = WorkflowTemplateService(catalog=available_catalog)

    template = await service.capture(
        db,
        _actor(test_org, test_user),
        _capture_request(template_source),
    )

    snapshot = TemplateSnapshot.model_validate(template.snapshot)
    assert template.version == 1
    assert template.name == "Saved"
    assert template.description is None
    assert snapshot.supervisor_agent_key == "a0"
    assert len(snapshot.agents) == 1
    assert snapshot.agents[0].system_prompt == "Answer"
    assert snapshot.max_parallelism == 2
    assert [(node.node_key, node.agent_key) for node in snapshot.nodes] == [
        ("research", "a0")
    ]
    assert snapshot.nodes[0].requires_confirmation is True
    assert snapshot.edges == []
    assert template_source.auto_routable is True
    assert template_source.description == "Source description"
    assert list(
        (
            await db.scalars(
                select(AccessGrant.id).where(AccessGrant.org_id == test_org.id)
            )
        ).all()
    ) == []


async def test_capture_preserves_edges_and_deduplicates_reused_agent(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    actor = _actor(test_org, test_user)
    agent = AgentDefinition(
        org_id=test_org.id,
        created_by=test_user.id,
        name="Shared agent",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    db.add(agent)
    await db.flush()
    source_snapshot = TemplateSnapshot.model_validate(
        {
            "schemaVersion": 1,
            "supervisorAgentKey": "a0",
            "maxParallelism": 2,
            "agents": [
                {
                    "key": "a0",
                    "name": "Shared agent",
                    "modelId": "provider/model-v1",
                    "systemPrompt": "Answer",
                    "temperature": 0.3,
                    "skillIds": [],
                    "knowledgeBaseIds": [],
                }
            ],
            "nodes": [
                {
                    "nodeKey": "first",
                    "displayName": "First",
                    "agentKey": "a0",
                    "instruction": "Start",
                    "positionX": 10.0,
                    "positionY": 20.0,
                    "requiresConfirmation": True,
                },
                {
                    "nodeKey": "fallback",
                    "displayName": "Fallback",
                    "agentKey": "a0",
                    "instruction": "Recover",
                    "requiresConfirmation": False,
                },
            ],
            "edges": [
                {
                    "sourceNodeKey": "first",
                    "targetNodeKey": "fallback",
                    "condition": "on_failure",
                }
            ],
        }
    )
    source = await WorkflowDefinitionRepository().create(
        db,
        actor,
        workflow_draft_from_snapshot(
            source_snapshot,
            {"a0": agent.id},
            name="Branching source",
            description=None,
        ),
    )

    template = await WorkflowTemplateService(catalog=available_catalog).capture(
        db,
        actor,
        _capture_request(source),
    )

    assert len(template.snapshot["agents"]) == 1
    assert [node["agentKey"] for node in template.snapshot["nodes"]] == [
        "a0",
        "a0",
    ]
    nodes_by_key = {
        node["nodeKey"]: node for node in template.snapshot["nodes"]
    }
    assert nodes_by_key["first"]["requiresConfirmation"] is True
    assert nodes_by_key["first"]["positionX"] == 10.0
    assert nodes_by_key["first"]["positionY"] == 20.0
    assert template.snapshot["edges"] == [
        {
            "sourceNodeKey": "first",
            "targetNodeKey": "fallback",
            "condition": "on_failure",
            "isLoopBack": False,
            "maxIterations": None,
        }
    ]


async def test_capture_ignores_later_source_changes(
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
            name="Saved",
            description=None,
        ),
    )
    source_agent = await db.get(
        AgentDefinition,
        template_source.supervisor_agent_definition_id,
    )
    assert source_agent is not None
    source_agent.system_prompt = "Changed"
    await db.flush()

    assert template.snapshot["agents"][0]["systemPrompt"] == "Answer"
    assert template.version == 1


async def test_capture_keeps_the_capabilities_switched_for_each_agent(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    source_agent = await db.get(AgentDefinition, template_source.supervisor_agent_definition_id)
    assert source_agent is not None
    source_agent.web_enabled = False
    source_agent.ask_user_enabled = False
    await db.flush()

    template = await WorkflowTemplateService(catalog=available_catalog).capture(
        db,
        _actor(test_org, test_user),
        TemplateCaptureRequest(
            source_workflow_id=template_source.id,
            source_workflow_version=1,
            name="Saved",
            description=None,
        ),
    )

    agent = template.snapshot["agents"][0]
    assert (agent["webEnabled"], agent["imagesEnabled"], agent["askUserEnabled"]) == (False, True, False)


async def test_capture_rejects_missing_and_foreign_sources(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
    template_snapshot: TemplateSnapshot,
) -> None:
    service = WorkflowTemplateService(catalog=available_catalog)
    actor = _actor(test_org, test_user)

    with pytest.raises(WorkflowTemplateError) as missing:
        await service.capture_source(db, actor, uuid4(), 1)
    _assert_error(missing, 404, "source_not_found")

    foreign_org = Organization(name="Foreign", slug=f"foreign-{uuid4().hex[:8]}")
    db.add(foreign_org)
    await db.flush()
    foreign_user = User(
        keycloak_id=uuid4(),
        email=f"foreign-{uuid4().hex[:8]}@example.com",
        org_id=foreign_org.id,
    )
    db.add(foreign_user)
    await db.flush()
    foreign_agent = AgentDefinition(
        org_id=foreign_org.id,
        created_by=foreign_user.id,
        name="Foreign agent",
        model_id="provider/model-v1",
        system_prompt="Answer",
    )
    db.add(foreign_agent)
    await db.flush()
    foreign_source = await WorkflowDefinitionRepository().create(
        db,
        AuthorizationSubject(foreign_user.id, foreign_org.id, "org_admin"),
        workflow_draft_from_snapshot(
            template_snapshot,
            {"a0": foreign_agent.id},
            name="Foreign source",
            description=None,
        ),
    )

    with pytest.raises(WorkflowTemplateError) as foreign:
        await service.capture_source(db, actor, foreign_source.id, 1)
    _assert_error(foreign, 404, "source_not_found")

    assert template_source.org_id == test_org.id


async def test_capture_rejects_archived_and_stale_source_versions(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    service = WorkflowTemplateService(catalog=available_catalog)
    actor = _actor(test_org, test_user)

    with pytest.raises(WorkflowTemplateError) as stale:
        await service.capture_source(db, actor, template_source.id, 2)
    _assert_error(stale, 409, "source_version_conflict")

    template_source.status = WorkflowDefinitionStatus.ARCHIVED.value
    await db.flush()
    with pytest.raises(WorkflowTemplateError) as archived:
        await service.capture_source(db, actor, template_source.id, 1)
    _assert_error(archived, 409, "source_archived")


async def test_replace_locks_template_first_and_updates_snapshot_and_version(
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
        _capture_request(template_source),
    )
    source_agent = await db.get(
        AgentDefinition,
        template_source.supervisor_agent_definition_id,
    )
    assert source_agent is not None
    source_agent.system_prompt = "Replacement answer"
    await db.flush()

    replaced = await service.replace(
        db,
        actor,
        template.id,
        TemplateReplaceRequest(
            source_workflow_id=template_source.id,
            source_workflow_version=1,
            version=1,
            name="Updated",
            description="Updated description",
        ),
    )

    assert replaced is template
    assert replaced.version == 2
    assert replaced.name == "Updated"
    assert replaced.description == "Updated description"
    assert replaced.snapshot["agents"][0]["systemPrompt"] == "Replacement answer"

    with pytest.raises(WorkflowTemplateError) as stale:
        await service.replace(
            db,
            actor,
            uuid4(),
            TemplateReplaceRequest(
                source_workflow_id=uuid4(),
                source_workflow_version=1,
                version=1,
                name="Not used",
                description=None,
            ),
        )
    _assert_error(stale, 404, "template_not_found")

    with pytest.raises(WorkflowTemplateError) as stale:
        await service.replace(
            db,
            actor,
            template.id,
            TemplateReplaceRequest(
                source_workflow_id=uuid4(),
                source_workflow_version=1,
                version=1,
                name="Not used",
                description=None,
            ),
        )
    _assert_error(stale, 409, "version_conflict")


async def test_replace_rejects_archived_template_before_reading_source(
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
        _capture_request(template_source),
    )
    template.status = "archived"
    await db.flush()

    with pytest.raises(WorkflowTemplateError) as caught:
        await service.replace(
            db,
            actor,
            template.id,
            TemplateReplaceRequest(
                source_workflow_id=uuid4(),
                source_workflow_version=1,
                version=1,
                name="Not used",
                description=None,
            ),
        )
    _assert_error(caught, 409, "template_archived")


async def test_capture_rejects_missing_dependencies(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    source_agent = await db.get(
        AgentDefinition,
        template_source.supervisor_agent_definition_id,
    )
    assert source_agent is not None
    source_agent.skill_ids = [uuid4()]
    await db.flush()

    with pytest.raises(WorkflowTemplateError) as caught:
        await WorkflowTemplateService(catalog=available_catalog).capture(
            db,
            _actor(test_org, test_user),
            _capture_request(template_source),
        )
    _assert_error(caught, 404, "dependency_unavailable")
    assert await db.scalar(select(WorkflowTemplate.id)) is None


async def test_org_admin_bypasses_local_dependency_grants(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    skill = Skill(org_id=test_org.id, name="Local skill", type="tool")
    knowledge_base = KnowledgeBase(org_id=test_org.id, name="Local knowledge")
    db.add_all([skill, knowledge_base])
    await db.flush()
    source_agent = await db.get(
        AgentDefinition,
        template_source.supervisor_agent_definition_id,
    )
    assert source_agent is not None
    source_agent.skill_ids = [skill.id]
    source_agent.knowledge_base_ids = [knowledge_base.id]
    await db.flush()

    template = await WorkflowTemplateService(catalog=available_catalog).capture(
        db,
        _actor(test_org, test_user),
        _capture_request(template_source),
    )

    assert template.snapshot["agents"][0]["skillIds"] == [str(skill.id)]
    assert template.snapshot["agents"][0]["knowledgeBaseIds"] == [
        str(knowledge_base.id)
    ]


async def test_org_admin_requires_real_read_grant_for_global_skill(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    global_skill = Skill(org_id=None, name="Global skill", type="tool")
    db.add(global_skill)
    await db.flush()
    source_agent = await db.get(
        AgentDefinition,
        template_source.supervisor_agent_definition_id,
    )
    assert source_agent is not None
    source_agent.skill_ids = [global_skill.id]
    await db.flush()
    service = WorkflowTemplateService(catalog=available_catalog)
    actor = _actor(test_org, test_user)

    with pytest.raises(WorkflowTemplateError) as denied:
        await service.capture(db, actor, _capture_request(template_source))
    _assert_error(denied, 404, "dependency_unavailable")

    db.add(
        AccessGrant(
            org_id=test_org.id,
            resource_type=ResourceType.SKILL.value,
            resource_id=str(global_skill.id),
            principal_type=PrincipalType.ORGANIZATION.value,
            principal_id=test_org.id,
            permission=AccessPermission.READ.value,
            created_via=GrantSource.PROVISIONING.value,
        )
    )
    await db.flush()

    template = await service.capture(db, actor, _capture_request(template_source))
    assert template.snapshot["agents"][0]["skillIds"] == [str(global_skill.id)]


@pytest.mark.parametrize(
    "status",
    [GatewayCatalogStatus.UNAVAILABLE, GatewayCatalogStatus.MOCK],
)
async def test_capture_rejects_untrusted_catalog_status(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
    status: GatewayCatalogStatus,
) -> None:
    async def catalog() -> GatewayCatalogSnapshot:
        return GatewayCatalogSnapshot(status, ({"id": "provider/model-v1"},))

    with pytest.raises(WorkflowTemplateError) as caught:
        await WorkflowTemplateService(catalog=catalog).capture(
            db,
            _actor(test_org, test_user),
            _capture_request(template_source),
        )
    _assert_error(caught, 503, "catalog_unavailable")


async def test_capture_maps_catalog_exception_to_unavailable(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    async def broken_catalog() -> GatewayCatalogSnapshot:
        raise RuntimeError("gateway details must not escape")

    with pytest.raises(WorkflowTemplateError) as caught:
        await WorkflowTemplateService(catalog=broken_catalog).capture(
            db,
            _actor(test_org, test_user),
            _capture_request(template_source),
        )
    _assert_error(caught, 503, "catalog_unavailable")


async def test_available_empty_catalog_is_dependency_unavailable(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
    template_source: WorkflowDefinition,
) -> None:
    async def empty_catalog() -> GatewayCatalogSnapshot:
        return GatewayCatalogSnapshot(GatewayCatalogStatus.AVAILABLE, ())

    with pytest.raises(WorkflowTemplateError) as caught:
        await WorkflowTemplateService(catalog=empty_catalog).capture(
            db,
            _actor(test_org, test_user),
            _capture_request(template_source),
        )
    _assert_error(caught, 404, "dependency_unavailable")
