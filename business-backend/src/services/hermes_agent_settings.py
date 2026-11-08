"""The settings of the Hermes agent definition a session works for.

A Hermes session belongs to at most one agent definition: the one a workflow node
named for the bound execution, or the one the conversation's workspace was
given to in chat. A session with no agent definition is the user's general
Hermes and keeps the organization's settings. Everything here is read live and
fails closed: an agent that is gone or archived stops its sessions instead of
quietly widening them.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AccessPermission, AuthorizationSubject, ResourceRef, ResourceType
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.hermes_session import HermesSession
from src.models.interaction import InteractionExecution
from src.services.hermes_access import HermesAccessDenied
from src.services.authorization import authorization_service


@dataclass(frozen=True)
class HermesAgentSettings:
    agent_id: UUID
    web_enabled: bool
    images_enabled: bool
    ask_user_enabled: bool
    egress_mode: str | None
    model_id: str | None
    skills_scope: str
    skill_ids: tuple[UUID, ...]
    knowledge_base_ids: tuple[UUID, ...]


async def agent_id_of_session(
    session: AsyncSession, subject: AuthorizationSubject, row: HermesSession
) -> UUID | None:
    if row.active_execution_id is not None:
        bound = await session.scalar(
            select(InteractionExecution.agent_definition_id).where(
                InteractionExecution.org_id == subject.org_id,
                InteractionExecution.id == row.active_execution_id,
            )
        )
        if bound is not None:
            return bound
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.org_id == subject.org_id, Conversation.id == row.conversation_id
        )
    )
    return conversation.hermes_agent_definition_id if conversation is not None else None


async def agent_settings_of_session(
    session: AsyncSession, subject: AuthorizationSubject, row: HermesSession
) -> HermesAgentSettings | None:
    """None for the general Hermes; raises HermesAccessDenied when the agent is gone."""
    agent_id = await agent_id_of_session(session, subject, row)
    if agent_id is None:
        return None
    agent = await session.scalar(
        select(AgentDefinition).where(
            AgentDefinition.org_id == subject.org_id,
            AgentDefinition.id == agent_id,
            AgentDefinition.kind == "hermes",
        )
    )
    if (
        agent is None
        or agent.archived_at is not None
        or not await authorization_service.can_access(
            session,
            subject,
            ResourceRef(ResourceType.AGENT_DEFINITION, str(agent.id)),
            AccessPermission.READ,
            resource_org_id=subject.org_id,
        )
    ):
        raise HermesAccessDenied("hermes_agent_unavailable")
    return HermesAgentSettings(
        agent_id=agent.id,
        web_enabled=agent.web_enabled,
        images_enabled=agent.images_enabled,
        ask_user_enabled=agent.ask_user_enabled,
        egress_mode=agent.hermes_egress_mode,
        model_id=None if agent.use_org_hermes_model else agent.model_id,
        skills_scope=agent.hermes_skills_scope,
        skill_ids=tuple(agent.skill_ids),
        knowledge_base_ids=tuple(agent.knowledge_base_ids),
    )
