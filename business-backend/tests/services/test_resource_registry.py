import uuid

from src.core.authorization_types import ResourceRef, ResourceType
from src.models.organization import Organization
from src.models.knowledge_base import KnowledgeBase
from src.models.agent_definition import AgentDefinition
from src.models.skill import Skill
from src.models.workflow_definition import WorkflowDefinition
from src.services.resource_registry import ResourceRegistry


async def test_model_lookup_is_exact_and_fails_closed_when_catalog_is_unavailable(db) -> None:
    async def catalog():
        return [{"id": "gateway/model:v1"}]

    async def unavailable():
        raise RuntimeError("gateway unavailable")

    assert await ResourceRegistry(model_catalog=catalog).exists(
        db,
        uuid.uuid4(),
        ResourceRef(ResourceType.MODEL, "gateway/model:v1"),
    )
    assert not await ResourceRegistry(model_catalog=catalog).exists(
        db,
        uuid.uuid4(),
        ResourceRef(ResourceType.MODEL, "gateway/model"),
    )
    assert not await ResourceRegistry(model_catalog=unavailable).exists(
        db,
        uuid.uuid4(),
        ResourceRef(ResourceType.MODEL, "gateway/model:v1"),
    )


async def test_skill_lookup_accepts_same_org_and_global_but_hides_foreign(db) -> None:
    async def unused_catalog() -> list[dict[str, object]]:
        return []

    org = Organization(name="Registry Org", slug=f"registry-{uuid.uuid4()}")
    other = Organization(name="Registry Other", slug=f"registry-other-{uuid.uuid4()}")
    db.add_all([org, other])
    await db.flush()
    local = Skill(org_id=org.id, name="local", type="tool")
    global_skill = Skill(org_id=None, name="global", type="tool")
    foreign = Skill(org_id=other.id, name="foreign", type="tool")
    db.add_all([local, global_skill, foreign])
    await db.commit()
    registry = ResourceRegistry(model_catalog=unused_catalog)

    assert await registry.exists(
        db, org.id, ResourceRef(ResourceType.SKILL, str(local.id))
    )
    assert await registry.exists(
        db, org.id, ResourceRef(ResourceType.SKILL, str(global_skill.id))
    )
    assert not await registry.exists(
        db, org.id, ResourceRef(ResourceType.SKILL, str(foreign.id))
    )


async def test_knowledge_base_lookup_accepts_only_canonical_same_org_rows(db) -> None:
    async def unused_catalog() -> list[dict[str, object]]:
        return []

    org = Organization(name="Knowledge Registry", slug=f"knowledge-registry-{uuid.uuid4()}")
    other = Organization(
        name="Knowledge Registry Other",
        slug=f"knowledge-registry-other-{uuid.uuid4()}",
    )
    db.add_all([org, other])
    await db.flush()
    local = KnowledgeBase(org_id=org.id, name="Local")
    foreign = KnowledgeBase(org_id=other.id, name="Foreign")
    db.add_all([local, foreign])
    await db.commit()
    registry = ResourceRegistry(model_catalog=unused_catalog)

    assert await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.KNOWLEDGE_BASE, str(local.id)),
    )
    assert not await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.KNOWLEDGE_BASE, str(foreign.id)),
    )
    assert not await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.KNOWLEDGE_BASE, "not-a-uuid"),
    )


async def test_agent_definition_lookup_accepts_only_canonical_same_org_rows(db) -> None:
    async def unused_catalog() -> list[dict[str, object]]:
        return []

    org = Organization(name="Agent Registry", slug=f"agent-registry-{uuid.uuid4()}")
    other = Organization(
        name="Agent Registry Other",
        slug=f"agent-registry-other-{uuid.uuid4()}",
    )
    db.add_all([org, other])
    await db.flush()
    local = AgentDefinition(
        org_id=org.id,
        name="Local agent",
        model_id="model",
        system_prompt="Answer",
    )
    foreign = AgentDefinition(
        org_id=other.id,
        name="Foreign agent",
        model_id="model",
        system_prompt="Answer",
    )
    db.add_all([local, foreign])
    await db.commit()
    registry = ResourceRegistry(model_catalog=unused_catalog)

    assert await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.AGENT_DEFINITION, str(local.id)),
    )
    assert not await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.AGENT_DEFINITION, str(foreign.id)),
    )


async def test_workflow_lookup_accepts_only_canonical_same_org_rows(db) -> None:
    async def unused_catalog() -> list[dict[str, object]]:
        return []

    org = Organization(name="Workflow Registry", slug=f"workflow-registry-{uuid.uuid4()}")
    other = Organization(
        name="Workflow Registry Other",
        slug=f"workflow-registry-other-{uuid.uuid4()}",
    )
    db.add_all([org, other])
    await db.flush()
    local_agent = AgentDefinition(
        org_id=org.id,
        name="Local supervisor",
        model_id="model",
        system_prompt="Answer",
    )
    foreign_agent = AgentDefinition(
        org_id=other.id,
        name="Foreign supervisor",
        model_id="model",
        system_prompt="Answer",
    )
    db.add_all([local_agent, foreign_agent])
    await db.flush()
    local = WorkflowDefinition(
        org_id=org.id,
        name="Local workflow",
        supervisor_agent_definition_id=local_agent.id,
    )
    foreign = WorkflowDefinition(
        org_id=other.id,
        name="Foreign workflow",
        supervisor_agent_definition_id=foreign_agent.id,
    )
    db.add_all([local, foreign])
    await db.commit()
    registry = ResourceRegistry(model_catalog=unused_catalog)

    assert await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.WORKFLOW, str(local.id)),
    )
    assert not await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.WORKFLOW, str(foreign.id)),
    )
    assert not await registry.exists(
        db,
        org.id,
        ResourceRef(ResourceType.WORKFLOW, "not-a-uuid"),
    )
