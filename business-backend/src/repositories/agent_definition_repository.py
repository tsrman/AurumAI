from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import String, cast, delete, or_, select
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
from src.models.interaction import InteractionExecution
from src.models.workflow_definition import WorkflowDefinition, WorkflowDefinitionStatus, WorkflowNode
from src.models.workflow_run import WorkflowNodeRun, WorkflowRun, WorkflowRunStatus
from src.schemas.agent_definition import AgentDefinitionCreate, AgentDefinitionUpdate
from src.services.authorization import authorization_service


@dataclass
class AgentReferences:
    """What still refers to an agent: the reasons it cannot be deleted or archived."""

    active_workflows: list[tuple[UUID, str]] = dataclass_field(default_factory=list)
    archived_workflows: list[tuple[UUID, str]] = dataclass_field(default_factory=list)
    in_active_run: bool = False
    has_history: bool = False


_OPEN_RUN_STATUSES = (
    WorkflowRunStatus.PENDING.value,
    WorkflowRunStatus.PLANNING.value,
    WorkflowRunStatus.RUNNING.value,
    WorkflowRunStatus.SYNTHESIZING.value,
)


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
            web_enabled=data.web_enabled,
            images_enabled=data.images_enabled,
            ask_user_enabled=data.ask_user_enabled,
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
        include_archived: bool = False,
    ) -> list[AgentDefinition]:
        access = authorization_service.accessible_resource_predicate(
            subject,
            ResourceType.AGENT_DEFINITION,
            AccessPermission.READ,
            cast(AgentDefinition.id, String),
        )
        query = select(AgentDefinition).where(AgentDefinition.org_id == subject.org_id, access)
        if not include_archived:
            query = query.where(AgentDefinition.archived_at.is_(None))
        result = await session.execute(
            query.order_by(AgentDefinition.created_at.desc(), AgentDefinition.id).limit(limit).offset(offset)
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
            "web_enabled",
            "images_enabled",
            "ask_user_enabled",
        ):
            if field in data.model_fields_set:
                setattr(agent, field, getattr(data, field))
        await session.commit()
        await session.refresh(agent)
        return agent

    async def references(self, session: AsyncSession, org_id: UUID, agent_id: UUID) -> AgentReferences:
        """Workflows (as supervisor or node), runs and interaction records that name the agent."""
        rows = await session.execute(
            select(WorkflowDefinition.id, WorkflowDefinition.name, WorkflowDefinition.status)
            .where(
                WorkflowDefinition.org_id == org_id,
                or_(
                    WorkflowDefinition.supervisor_agent_definition_id == agent_id,
                    WorkflowDefinition.id.in_(
                        select(WorkflowNode.workflow_definition_id).where(
                            WorkflowNode.org_id == org_id, WorkflowNode.agent_definition_id == agent_id
                        )
                    ),
                ),
            )
            .order_by(WorkflowDefinition.name, WorkflowDefinition.id)
        )
        refs = AgentReferences()
        for workflow_id, name, status in rows:
            target = (
                refs.archived_workflows
                if status == WorkflowDefinitionStatus.ARCHIVED.value
                else refs.active_workflows
            )
            target.append((workflow_id, name))
        refs.in_active_run = (
            await session.scalar(
                select(WorkflowNodeRun.id)
                .join(WorkflowRun, WorkflowRun.id == WorkflowNodeRun.workflow_run_id)
                .where(
                    WorkflowNodeRun.org_id == org_id,
                    WorkflowNodeRun.agent_definition_id == agent_id,
                    WorkflowRun.status.in_(_OPEN_RUN_STATUSES),
                )
                .limit(1)
            )
            is not None
        )
        refs.has_history = (
            await session.scalar(
                select(WorkflowNodeRun.id)
                .where(WorkflowNodeRun.org_id == org_id, WorkflowNodeRun.agent_definition_id == agent_id)
                .limit(1)
            )
            is not None
            or await session.scalar(
                select(InteractionExecution.id)
                .where(
                    InteractionExecution.org_id == org_id,
                    InteractionExecution.agent_definition_id == agent_id,
                )
                .limit(1)
            )
            is not None
        )
        return refs

    async def set_archived(
        self,
        session: AsyncSession,
        agent: AgentDefinition,
        subject: AuthorizationSubject,
        *,
        archived: bool,
    ) -> AgentDefinition:
        if (agent.archived_at is not None) != archived:
            agent.archived_at = datetime.now(timezone.utc) if archived else None
            agent.archived_by = subject.user_id if archived else None
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
