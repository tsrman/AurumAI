from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from src.models.workflow_template import WorkflowTemplate, WorkflowTemplateStatus
from src.repositories.workflow_definition_repository import (
    workflow_definition_repository,
)
from src.repositories.workflow_template_repository import WorkflowTemplateRepository
from src.schemas.workflow_template import (
    TemplateCaptureRequest,
    TemplateInstantiateRequest,
    TemplateReplaceRequest,
    TemplateSnapshot,
    TemplateVersionRequest,
)
from src.services.agent_authorization import (
    AgentAuthorizationService,
    AgentDependencyAccessDenied,
)
from src.services.model_catalog import (
    GatewayCatalogSnapshot,
    GatewayCatalogStatus,
    gateway_catalog_snapshot,
)
from src.services.resource_registry import ResourceRegistry
from src.services.workflow_template_errors import WorkflowTemplateError
from src.services.workflow_template_snapshot import (
    build_template_snapshot,
    template_agent_name,
    workflow_draft_from_snapshot,
)


CatalogLoader = Callable[[], Awaitable[GatewayCatalogSnapshot]]


def _validate_current_limits(snapshot: TemplateSnapshot) -> None:
    # Stored format validity is stable; only new captures/copies must fit the
    # operator's current execution budget. Check before any persistent mutation.
    if (
        len(snapshot.nodes) > settings.WORKFLOW_MAX_NODES
        or snapshot.max_parallelism > settings.WORKFLOW_MAX_PARALLELISM
    ):
        raise WorkflowTemplateError("snapshot_invalid", 422)


@dataclass(frozen=True, slots=True)
class InstantiatedWorkflow:
    workflow: WorkflowDefinition
    agent_ids: list[UUID]


class WorkflowTemplateService:
    def __init__(
        self,
        catalog: CatalogLoader = gateway_catalog_snapshot,
    ) -> None:
        self._catalog = catalog
        self._repository = WorkflowTemplateRepository()

    async def capture(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        data: TemplateCaptureRequest,
    ) -> WorkflowTemplate:
        snapshot = await self.capture_source(
            session,
            actor,
            data.source_workflow_id,
            data.source_workflow_version,
        )
        await self.validate_dependencies(session, actor, snapshot)
        return await self._repository.create(
            session,
            actor,
            name=data.name,
            description=data.description,
            snapshot=snapshot,
        )

    async def replace(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        template_id: UUID,
        data: TemplateReplaceRequest,
    ) -> WorkflowTemplate:
        template = await self._repository.get(
            session,
            actor.org_id,
            template_id,
            for_update=True,
        )
        if template is None:
            raise WorkflowTemplateError("template_not_found", 404)
        if template.version != data.version:
            raise WorkflowTemplateError("version_conflict", 409)
        if template.status == WorkflowTemplateStatus.ARCHIVED.value:
            raise WorkflowTemplateError("template_archived", 409)

        snapshot = await self.capture_source(
            session,
            actor,
            data.source_workflow_id,
            data.source_workflow_version,
        )
        await self.validate_dependencies(session, actor, snapshot)
        return await self._repository.replace_locked(
            session,
            actor,
            template,
            version=data.version,
            name=data.name,
            description=data.description,
            snapshot=snapshot,
        )

    async def instantiate(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        template_id: UUID,
        data: TemplateInstantiateRequest,
    ) -> InstantiatedWorkflow:
        template = await self._repository.get(
            session,
            actor.org_id,
            template_id,
            for_update=True,
        )
        if template is None:
            raise WorkflowTemplateError("template_not_found", 404)
        if template.version != data.version:
            raise WorkflowTemplateError("version_conflict", 409)
        if template.status == WorkflowTemplateStatus.ARCHIVED.value:
            raise WorkflowTemplateError("template_archived", 409)

        snapshot = TemplateSnapshot.model_validate(template.snapshot)
        _validate_current_limits(snapshot)
        await self.validate_dependencies(session, actor, snapshot)
        agent_ids = await self.create_agents(session, actor, snapshot)
        workflow = await workflow_definition_repository.create(
            session,
            actor,
            workflow_draft_from_snapshot(
                snapshot,
                agent_ids,
                name=data.name,
                description=template.description,
            ),
        )
        workflow.source_template_id = template.id
        workflow.source_template_version = template.version
        await session.flush()
        return InstantiatedWorkflow(
            workflow=workflow,
            agent_ids=list(agent_ids.values()),
        )

    async def create_agents(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        snapshot: TemplateSnapshot,
    ) -> dict[str, UUID]:
        agent_ids: dict[str, UUID] = {}
        for config in snapshot.agents:
            new_id = uuid4()
            session.add(
                AgentDefinition(
                    id=new_id,
                    org_id=actor.org_id,
                    created_by=actor.user_id,
                    name=template_agent_name(config.name, new_id),
                    description=config.description,
                    model_id=config.model_id,
                    system_prompt=config.system_prompt,
                    temperature=config.temperature,
                    skill_ids=list(config.skill_ids),
                    knowledge_base_ids=list(config.knowledge_base_ids),
                    kind=config.kind,
                    web_enabled=config.web_enabled,
                    images_enabled=config.images_enabled,
                    ask_user_enabled=config.ask_user_enabled,
                )
            )
            agent_ids[config.key] = new_id
        await session.flush()
        return agent_ids

    async def archive(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        template_id: UUID,
        data: TemplateVersionRequest,
    ) -> WorkflowTemplate:
        template = await self._repository.get(
            session,
            actor.org_id,
            template_id,
            for_update=True,
        )
        if template is None:
            raise WorkflowTemplateError("template_not_found", 404)
        return await self._repository.archive_locked(
            session,
            actor,
            template,
            version=data.version,
        )

    async def validate_dependencies(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        snapshot: TemplateSnapshot,
    ) -> None:
        try:
            catalog_snapshot = await self._catalog()
        except Exception:
            raise WorkflowTemplateError("catalog_unavailable", 503) from None
        if catalog_snapshot.status is not GatewayCatalogStatus.AVAILABLE:
            raise WorkflowTemplateError("catalog_unavailable", 503)

        async def request_catalog() -> list[dict[str, object]]:
            return list(catalog_snapshot.models)

        authorization = AgentAuthorizationService(
            registry=ResourceRegistry(model_catalog=request_catalog)
        )
        try:
            for agent in snapshot.agents:
                await authorization.require_dependencies(
                    session,
                    actor,
                    agent_id=uuid5(NAMESPACE_URL, agent.key),
                    model_id=agent.model_id,
                    skill_ids=agent.skill_ids,
                    knowledge_base_ids=agent.knowledge_base_ids,
                )
        except AgentDependencyAccessDenied:
            raise WorkflowTemplateError("dependency_unavailable", 404) from None

    async def capture_source(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        source_id: UUID,
        source_version: int,
    ) -> TemplateSnapshot:
        source = (
            await session.execute(
                select(WorkflowDefinition)
                .where(
                    WorkflowDefinition.id == source_id,
                    WorkflowDefinition.org_id == actor.org_id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if source is None:
            raise WorkflowTemplateError("source_not_found", 404)
        if source.status == WorkflowDefinitionStatus.ARCHIVED.value:
            raise WorkflowTemplateError("source_archived", 409)
        if source.version != source_version:
            raise WorkflowTemplateError("source_version_conflict", 409)

        nodes, edges = await workflow_definition_repository.load_graph(
            session,
            actor.org_id,
            source.id,
        )
        referenced_agent_ids = {
            source.supervisor_agent_definition_id,
            *(node.agent_definition_id for node in nodes),
        }
        agents = list(
            (
                await session.execute(
                    select(AgentDefinition)
                    .where(
                        AgentDefinition.org_id == actor.org_id,
                        AgentDefinition.id.in_(referenced_agent_ids),
                    )
                    .order_by(AgentDefinition.id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            ).scalars()
        )
        if {agent.id for agent in agents} != referenced_agent_ids:
            raise WorkflowTemplateError("source_not_found", 404)

        snapshot = build_template_snapshot(source, nodes, edges, agents)
        _validate_current_limits(snapshot)
        return snapshot
