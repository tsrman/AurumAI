from uuid import UUID

from sqlalchemy import String, cast, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    GrantSource,
    PrincipalType,
    ResourceType,
)
from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.schemas.agent_definition import AgentDefinitionCreate, AgentDefinitionUpdate
from src.services.authorization import authorization_service


class AgentDefinitionRepository:
    async def create_with_owner_write_grant(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        data: AgentDefinitionCreate,
    ) -> AgentDefinition:
        agent = AgentDefinition(
            org_id=subject.org_id,
            created_by=subject.user_id,
            name=data.name,
            description=data.description,
            model_id=data.model_id,
            system_prompt=data.system_prompt,
            temperature=data.temperature,
            skill_ids=data.skill_ids,
            knowledge_base_ids=data.knowledge_base_ids,
            kind=data.kind,
        )
        session.add(agent)
        await session.flush()
        session.add(
            AccessGrant(
                org_id=subject.org_id,
                resource_type=ResourceType.AGENT_DEFINITION.value,
                resource_id=str(agent.id),
                principal_type=PrincipalType.USER.value,
                principal_id=subject.user_id,
                permission=AccessPermission.WRITE.value,
                created_via=GrantSource.PROVISIONING.value,
            )
        )
        await session.commit()
        await session.refresh(agent)
        return agent

    async def list_by_org(
        self,
        session: AsyncSession,
        org_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentDefinition]:
        result = await session.execute(
            select(AgentDefinition)
            .where(AgentDefinition.org_id == org_id)
            .order_by(AgentDefinition.created_at.desc(), AgentDefinition.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def list_accessible(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentDefinition]:
        access = authorization_service.accessible_resource_predicate(
            subject,
            ResourceType.AGENT_DEFINITION,
            AccessPermission.READ,
            cast(AgentDefinition.id, String),
        )
        result = await session.execute(
            select(AgentDefinition)
            .where(AgentDefinition.org_id == subject.org_id, access)
            .order_by(AgentDefinition.created_at.desc(), AgentDefinition.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def get_by_id(
        self,
        session: AsyncSession,
        org_id: UUID,
        agent_id: UUID,
    ) -> AgentDefinition | None:
        result = await session.execute(
            select(AgentDefinition).where(
                AgentDefinition.id == agent_id,
                AgentDefinition.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_accessible(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        agent_id: UUID,
        permission: AccessPermission = AccessPermission.READ,
    ) -> AgentDefinition | None:
        access = authorization_service.accessible_resource_predicate(
            subject,
            ResourceType.AGENT_DEFINITION,
            permission,
            cast(AgentDefinition.id, String),
        )
        result = await session.execute(
            select(AgentDefinition).where(
                AgentDefinition.id == agent_id,
                AgentDefinition.org_id == subject.org_id,
                access,
            )
        )
        return result.scalar_one_or_none()

    async def update(
        self,
        session: AsyncSession,
        agent: AgentDefinition,
        data: AgentDefinitionUpdate,
    ) -> AgentDefinition:
        for field in (
            "name",
            "description",
            "model_id",
            "system_prompt",
            "temperature",
            "skill_ids",
            "knowledge_base_ids",
        ):
            if field in data.model_fields_set:
                setattr(agent, field, getattr(data, field))
        await session.commit()
        await session.refresh(agent)
        return agent

    async def delete_with_grants(
        self,
        session: AsyncSession,
        agent: AgentDefinition,
    ) -> None:
        await session.execute(
            delete(AccessGrant).where(
                AccessGrant.org_id == agent.org_id,
                AccessGrant.resource_type == ResourceType.AGENT_DEFINITION.value,
                AccessGrant.resource_id == str(agent.id),
            )
        )
        await session.delete(agent)
        await session.commit()


agent_definition_repository = AgentDefinitionRepository()
