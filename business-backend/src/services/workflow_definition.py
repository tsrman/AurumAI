from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import WorkflowDefinition
from src.repositories.workflow_definition_repository import (
    WorkflowDefinitionRepository,
    workflow_definition_repository,
)
from src.schemas.workflow import (
    WorkflowDefinitionCreate,
    WorkflowDefinitionDraft,
    WorkflowDefinitionReplace,
)
from src.services.agent_authorization import (
    AgentAuthorizationService,
    AgentDependencyAccessDenied,
    agent_authorization_service,
)
from src.services.workflow_graph import ValidatedWorkflowGraph, WorkflowGraphError
from src.services.workflow_graph import validate_workflow_graph


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionNotFound(Exception):
    workflow_id: UUID


@dataclass(frozen=True, slots=True)
class WorkflowDependencyNotFound(Exception):
    resource_id: UUID


class WorkflowDefinitionService:
    def __init__(
        self,
        *,
        repository: WorkflowDefinitionRepository = workflow_definition_repository,
        agent_authorization: AgentAuthorizationService = agent_authorization_service,
    ) -> None:
        self._repository = repository
        self._agent_authorization = agent_authorization

    async def create(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        data: WorkflowDefinitionCreate,
    ) -> WorkflowDefinition:
        await self.validate_draft(session, actor, data)
        return await self._repository.create(session, actor, data)

    async def replace(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        workflow_id: UUID,
        data: WorkflowDefinitionReplace,
    ) -> WorkflowDefinition:
        await self._validate(session, actor, data)
        try:
            return await self._repository.replace(session, actor, workflow_id, data)
        except LookupError as exc:
            raise WorkflowDefinitionNotFound(workflow_id) from exc

    async def archive(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        workflow_id: UUID,
    ) -> None:
        definition = await self._repository.archive(session, actor, workflow_id)
        if definition is None:
            raise WorkflowDefinitionNotFound(workflow_id)

    async def validate_draft(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        data: WorkflowDefinitionCreate,
    ) -> ValidatedWorkflowGraph:
        return await self._validate(session, actor, data)

    async def _validate(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        data: WorkflowDefinitionDraft,
    ) -> ValidatedWorkflowGraph:
        graph = validate_workflow_graph(
            data.nodes,
            data.edges,
            max_nodes=settings.WORKFLOW_MAX_NODES,
        )
        if data.max_parallelism > settings.WORKFLOW_MAX_PARALLELISM:
            raise WorkflowGraphError("parallelism_limit")

        agent_ids = {
            data.supervisor_agent_definition_id,
            *(node.agent_definition_id for node in data.nodes),
        }
        agents = list(
            (
                await session.execute(
                    select(AgentDefinition).where(
                        AgentDefinition.org_id == actor.org_id,
                        AgentDefinition.id.in_(agent_ids),
                        # An archived agent is gone for new use, like a missing one.
                        AgentDefinition.archived_at.is_(None),
                    )
                )
            ).scalars()
        )
        agents_by_id = {agent.id: agent for agent in agents}
        missing = sorted(agent_ids - set(agents_by_id), key=str)
        if missing:
            raise WorkflowDependencyNotFound(missing[0])
        # The supervisor plans and synthesizes in the text protocol; Hermes
        # Agent runs nodes only.
        supervisor = agents_by_id[data.supervisor_agent_definition_id]
        if supervisor.kind != "agent":
            raise WorkflowGraphError("hermes_supervisor")

        for agent_id in sorted(agent_ids, key=str):
            agent = agents_by_id[agent_id]
            try:
                await self._agent_authorization.require_dependencies_exist(
                    session,
                    actor,
                    agent_id=agent.id,
                    model_id=agent.model_id,
                    skill_ids=agent.skill_ids,
                    knowledge_base_ids=agent.knowledge_base_ids,
                )
            except AgentDependencyAccessDenied as exc:
                raise WorkflowDependencyNotFound(agent.id) from exc
        return graph


workflow_definition_service = WorkflowDefinitionService()
