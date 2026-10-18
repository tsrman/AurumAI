import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    GrantSource,
    PrincipalType,
)
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.group import Group
from src.models.knowledge_base import KnowledgeBase
from src.models.organization import Organization
from src.models.skill import Skill
from src.models.user import User
from src.models.user_group import UserGroup
from src.services.agent_authorization import (
    AgentAuthorizationService,
    AgentDependencyAccessDenied,
)
from src.services.resource_registry import ResourceRegistry


def _grant(
    *,
    org_id: uuid.UUID,
    resource_type: str,
    resource_id: str,
    principal_type: PrincipalType,
    principal_id: uuid.UUID,
) -> AccessGrant:
    return AccessGrant(
        org_id=org_id,
        resource_type=resource_type,
        resource_id=resource_id,
        principal_type=principal_type.value,
        principal_id=principal_id,
        permission=AccessPermission.READ.value,
        created_via=GrantSource.PROVISIONING.value,
    )


async def _model_catalog() -> list[dict[str, object]]:
    return [{"id": "provider/model-v1"}]


async def test_agent_grant_does_not_amplify_dependency_access_and_revoke_is_live(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    skill = Skill(org_id=test_org.id, name="Search", type="tool")
    knowledge_base = KnowledgeBase(org_id=test_org.id, name="Policies")
    db.add_all([skill, knowledge_base])
    await db.flush()
    agent = AgentDefinition(
        org_id=test_org.id,
        created_by=test_user.id,
        name="Research",
        model_id="provider/model-v1",
        system_prompt="Find evidence",
        skill_ids=[skill.id],
        knowledge_base_ids=[knowledge_base.id],
    )
    db.add(agent)
    await db.flush()
    agent_grant = _grant(
        org_id=test_org.id,
        resource_type="agent_definition",
        resource_id=str(agent.id),
        principal_type=PrincipalType.USER,
        principal_id=test_user.id,
    )
    db.add(agent_grant)
    await db.commit()

    subject = AuthorizationSubject(user_id=test_user.id, org_id=test_org.id)
    service = AgentAuthorizationService(
        registry=ResourceRegistry(model_catalog=_model_catalog)
    )
    with pytest.raises(AgentDependencyAccessDenied):
        await service.require_executable(db, subject, agent.id)

    dependency_grants = [
        _grant(
            org_id=test_org.id,
            resource_type=resource_type,
            resource_id=resource_id,
            principal_type=PrincipalType.USER,
            principal_id=test_user.id,
        )
        for resource_type, resource_id in (
            ("model", agent.model_id),
            ("skill", str(skill.id)),
            ("knowledge_base", str(knowledge_base.id)),
        )
    ]
    db.add_all(dependency_grants)
    await db.commit()
    assert await service.require_executable(db, subject, agent.id) == agent

    await db.execute(
        delete(AccessGrant).where(AccessGrant.id == dependency_grants[1].id)
    )
    await db.commit()
    with pytest.raises(AgentDependencyAccessDenied):
        await service.require_executable(db, subject, agent.id)


async def test_multi_group_union_must_cover_every_dependency(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    group_a = Group(org_id=test_org.id, name="Agent readers")
    group_b = Group(org_id=test_org.id, name="Data readers")
    skill = Skill(org_id=test_org.id, name="Lookup", type="tool")
    knowledge_base = KnowledgeBase(org_id=test_org.id, name="Manuals")
    db.add_all([group_a, group_b, skill, knowledge_base])
    await db.flush()
    db.add_all(
        [
            UserGroup(user_id=test_user.id, group_id=group_a.id),
            UserGroup(user_id=test_user.id, group_id=group_b.id),
        ]
    )
    agent = AgentDefinition(
        org_id=test_org.id,
        name="Union",
        model_id="provider/model-v1",
        system_prompt="Answer",
        skill_ids=[skill.id],
        knowledge_base_ids=[knowledge_base.id],
    )
    db.add(agent)
    await db.flush()
    grants = [
        _grant(
            org_id=test_org.id,
            resource_type="agent_definition",
            resource_id=str(agent.id),
            principal_type=PrincipalType.GROUP,
            principal_id=group_a.id,
        ),
        _grant(
            org_id=test_org.id,
            resource_type="model",
            resource_id=agent.model_id,
            principal_type=PrincipalType.GROUP,
            principal_id=group_a.id,
        ),
        _grant(
            org_id=test_org.id,
            resource_type="skill",
            resource_id=str(skill.id),
            principal_type=PrincipalType.GROUP,
            principal_id=group_a.id,
        ),
        _grant(
            org_id=test_org.id,
            resource_type="knowledge_base",
            resource_id=str(knowledge_base.id),
            principal_type=PrincipalType.GROUP,
            principal_id=group_b.id,
        ),
    ]
    db.add_all(grants)
    await db.commit()

    service = AgentAuthorizationService(
        registry=ResourceRegistry(model_catalog=_model_catalog)
    )
    subject = AuthorizationSubject(user_id=test_user.id, org_id=test_org.id)
    assert await service.require_executable(db, subject, agent.id) == agent

    await db.execute(delete(AccessGrant).where(AccessGrant.id == grants[-1].id))
    await db.commit()
    with pytest.raises(AgentDependencyAccessDenied):
        await service.require_executable(db, subject, agent.id)


async def test_org_admin_bypasses_local_dependencies_but_not_global_skill(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    global_skill = Skill(org_id=None, name="Operator published", type="tool")
    db.add(global_skill)
    await db.flush()
    agent = AgentDefinition(
        org_id=test_org.id,
        name="Admin agent",
        model_id="provider/model-v1",
        system_prompt="Answer",
        skill_ids=[global_skill.id],
    )
    db.add(agent)
    await db.commit()
    service = AgentAuthorizationService(
        registry=ResourceRegistry(model_catalog=_model_catalog)
    )
    subject = AuthorizationSubject(
        user_id=test_user.id,
        org_id=test_org.id,
        role="org_admin",
    )

    with pytest.raises(AgentDependencyAccessDenied):
        await service.require_executable(db, subject, agent.id)

    db.add(
        _grant(
            org_id=test_org.id,
            resource_type="skill",
            resource_id=str(global_skill.id),
            principal_type=PrincipalType.ORGANIZATION,
            principal_id=test_org.id,
        )
    )
    await db.commit()
    assert await service.require_executable(db, subject, agent.id) == agent
