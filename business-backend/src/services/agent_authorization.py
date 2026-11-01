from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceRef,
    ResourceType,
)
from src.models.agent_definition import AgentDefinition
from src.models.knowledge_base import KnowledgeBase
from src.models.skill import Skill
from src.repositories.agent_definition_repository import (
    AgentDefinitionRepository,
    agent_definition_repository,
)
from src.services.authorization import AuthorizationService, authorization_service
from src.services.resource_registry import ResourceRegistry, resource_registry


@dataclass(frozen=True, slots=True)
class AgentDefinitionNotFound(Exception):
    agent_id: UUID


@dataclass(frozen=True, slots=True)
class AgentDependencyAccessDenied(Exception):
    agent_id: UUID


@dataclass(frozen=True, slots=True)
class ChatAgentUnsupported(Exception):
    """The agent cannot run a chat turn (Hermes-kind agents are workflow nodes only)."""

    kind: str


@dataclass(frozen=True, slots=True)
class ChatAgentTurn:
    """What a chat turn takes from an agent, resolved fresh for each turn."""

    id: UUID
    name: str
    model_id: str
    system_prompt: str
    temperature: float | None
    allowed_skill_ids: list[str]
    knowledge_bases: list[dict[str, str]]


class AgentAuthorizationService:
    def __init__(
        self,
        *,
        repository: AgentDefinitionRepository = agent_definition_repository,
        authorization: AuthorizationService = authorization_service,
        registry: ResourceRegistry = resource_registry,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._registry = registry

    async def _load_dependencies(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        *,
        agent_id: UUID,
        model_id: str,
        skill_ids: list[UUID],
        knowledge_base_ids: list[UUID],
    ) -> dict[UUID, UUID | None]:
        if not await self._registry.exists(
            session,
            subject.org_id,
            ResourceRef(ResourceType.MODEL, model_id),
        ):
            raise AgentDependencyAccessDenied(agent_id)

        skill_rows = (
            await session.execute(
                select(Skill.id, Skill.org_id).where(
                    Skill.id.in_(skill_ids),
                    or_(Skill.org_id == subject.org_id, Skill.org_id.is_(None)),
                )
            )
        ).all()
        skill_orgs = {row.id: row.org_id for row in skill_rows}
        if set(skill_orgs) != set(skill_ids):
            raise AgentDependencyAccessDenied(agent_id)

        base_ids = set(
            (
                await session.execute(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.id.in_(knowledge_base_ids),
                        KnowledgeBase.org_id == subject.org_id,
                    )
                )
            ).scalars()
        )
        if base_ids != set(knowledge_base_ids):
            raise AgentDependencyAccessDenied(agent_id)

        return skill_orgs

    async def require_dependencies_exist(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        *,
        agent_id: UUID,
        model_id: str,
        skill_ids: list[UUID],
        knowledge_base_ids: list[UUID],
    ) -> None:
        await self._load_dependencies(
            session,
            subject,
            agent_id=agent_id,
            model_id=model_id,
            skill_ids=skill_ids,
            knowledge_base_ids=knowledge_base_ids,
        )

    async def require_dependencies(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        *,
        agent_id: UUID,
        model_id: str,
        skill_ids: list[UUID],
        knowledge_base_ids: list[UUID],
    ) -> None:
        skill_orgs = await self._load_dependencies(
            session,
            subject,
            agent_id=agent_id,
            model_id=model_id,
            skill_ids=skill_ids,
            knowledge_base_ids=knowledge_base_ids,
        )

        if subject.is_org_admin:
            model_access = {model_id}
            base_access = {str(item) for item in knowledge_base_ids}
            local_skills = {
                str(skill_id)
                for skill_id, org_id in skill_orgs.items()
                if org_id == subject.org_id
            }
            non_admin_subject = AuthorizationSubject(
                user_id=subject.user_id,
                org_id=subject.org_id,
                role="member",
            )
            global_skill_ids = [
                str(skill_id)
                for skill_id, org_id in skill_orgs.items()
                if org_id is None
            ]
            global_skill_access = await self._authorization.accessible_resource_ids(
                session,
                non_admin_subject,
                ResourceType.SKILL,
                AccessPermission.READ,
                global_skill_ids,
            )
            skill_access = local_skills | global_skill_access
        else:
            model_access = await self._authorization.accessible_resource_ids(
                session,
                subject,
                ResourceType.MODEL,
                AccessPermission.READ,
                [model_id],
            )
            skill_access = await self._authorization.accessible_resource_ids(
                session,
                subject,
                ResourceType.SKILL,
                AccessPermission.READ,
                [str(item) for item in skill_ids],
            )
            base_access = await self._authorization.accessible_resource_ids(
                session,
                subject,
                ResourceType.KNOWLEDGE_BASE,
                AccessPermission.READ,
                [str(item) for item in knowledge_base_ids],
            )

        if (
            model_access != {model_id}
            or skill_access != {str(item) for item in skill_ids}
            or base_access != {str(item) for item in knowledge_base_ids}
        ):
            raise AgentDependencyAccessDenied(agent_id)

    async def require_executable(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        agent_id: UUID,
    ) -> AgentDefinition:
        agent = await self._repository.get_accessible(
            session,
            subject,
            agent_id,
            AccessPermission.READ,
        )
        # An archived agent is gone for new use, exactly like a missing one.
        if agent is None or agent.archived_at is not None:
            raise AgentDefinitionNotFound(agent_id)
        await self.require_dependencies(
            session,
            subject,
            agent_id=agent.id,
            model_id=agent.model_id,
            skill_ids=agent.skill_ids,
            knowledge_base_ids=agent.knowledge_base_ids,
        )
        return agent

    async def require_chat_agent(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        agent_id: UUID,
    ) -> ChatAgentTurn:
        """The agent a user may chat with now: granted, active, its dependencies accessible."""
        agent = await self.require_executable(session, subject, agent_id)
        if agent.kind != "agent":
            raise ChatAgentUnsupported(agent.kind)
        bases = {}
        if agent.knowledge_base_ids:
            rows = await session.execute(
                select(KnowledgeBase.id, KnowledgeBase.name).where(
                    KnowledgeBase.org_id == subject.org_id,
                    KnowledgeBase.id.in_(agent.knowledge_base_ids),
                )
            )
            bases = {row.id: row.name for row in rows}
        return ChatAgentTurn(
            id=agent.id,
            name=agent.name,
            model_id=agent.model_id,
            system_prompt=agent.system_prompt,
            temperature=agent.temperature,
            allowed_skill_ids=[str(item) for item in agent.skill_ids],
            knowledge_bases=[
                {"id": str(base_id), "name": bases[base_id]} for base_id in agent.knowledge_base_ids if base_id in bases
            ],
        )


agent_authorization_service = AgentAuthorizationService()
