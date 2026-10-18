from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import String, cast, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceType,
)
from src.models.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionStatus,
    WorkflowEdge,
    WorkflowNode,
)
from src.schemas.workflow import WorkflowDefinitionCreate, WorkflowDefinitionReplace
from src.services.authorization import authorization_service


@dataclass(frozen=True, slots=True)
class WorkflowVersionConflict(Exception):
    expected: int
    actual: int


class WorkflowDefinitionRepository:
    async def create(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        data: WorkflowDefinitionCreate,
    ) -> WorkflowDefinition:
        definition = WorkflowDefinition(
            org_id=actor.org_id,
            created_by=actor.user_id,
            updated_by=actor.user_id,
            name=data.name,
            description=data.description,
            supervisor_agent_definition_id=data.supervisor_agent_definition_id,
            max_parallelism=data.max_parallelism,
            auto_routable=data.auto_routable,
        )
        session.add(definition)
        await session.flush()
        await self._insert_graph(session, actor.org_id, definition.id, data)
        await session.flush()
        return definition

    async def replace(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        workflow_id: UUID,
        data: WorkflowDefinitionReplace,
    ) -> WorkflowDefinition:
        definition = await self.get_by_id(
            session, actor.org_id, workflow_id, for_update=True
        )
        if definition is None:
            raise LookupError(workflow_id)
        if definition.version != data.version:
            raise WorkflowVersionConflict(data.version, definition.version)

        await session.execute(
            delete(WorkflowEdge).where(
                WorkflowEdge.org_id == actor.org_id,
                WorkflowEdge.workflow_definition_id == workflow_id,
            )
        )
        await session.execute(
            delete(WorkflowNode).where(
                WorkflowNode.org_id == actor.org_id,
                WorkflowNode.workflow_definition_id == workflow_id,
            )
        )
        definition.name = data.name
        definition.description = data.description
        definition.supervisor_agent_definition_id = (
            data.supervisor_agent_definition_id
        )
        definition.max_parallelism = data.max_parallelism
        definition.auto_routable = data.auto_routable
        definition.updated_by = actor.user_id
        definition.version += 1
        await self._insert_graph(session, actor.org_id, definition.id, data)
        await session.flush()
        return definition

    async def archive(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        workflow_id: UUID,
    ) -> WorkflowDefinition | None:
        definition = await self.get_by_id(
            session, actor.org_id, workflow_id, for_update=True
        )
        if definition is None:
            return None
        if definition.status != WorkflowDefinitionStatus.ARCHIVED.value:
            definition.status = WorkflowDefinitionStatus.ARCHIVED.value
            definition.updated_by = actor.user_id
            definition.version += 1
            await session.flush()
        return definition

    async def get_by_id(
        self,
        session: AsyncSession,
        org_id: UUID,
        workflow_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkflowDefinition | None:
        statement = select(WorkflowDefinition).where(
            WorkflowDefinition.id == workflow_id,
            WorkflowDefinition.org_id == org_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await session.execute(statement)
        return result.scalar_one_or_none()

    async def list_by_org(
        self,
        session: AsyncSession,
        org_id: UUID,
        *,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkflowDefinition]:
        statement = select(WorkflowDefinition).where(
            WorkflowDefinition.org_id == org_id
        )
        if not include_archived:
            statement = statement.where(
                WorkflowDefinition.status == WorkflowDefinitionStatus.ACTIVE.value
            )
        result = await session.execute(
            statement.order_by(WorkflowDefinition.name, WorkflowDefinition.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def list_accessible(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        *,
        auto_routable_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkflowDefinition]:
        access = authorization_service.accessible_resource_predicate(
            subject,
            ResourceType.WORKFLOW,
            AccessPermission.READ,
            cast(WorkflowDefinition.id, String),
        )
        conditions = [
            WorkflowDefinition.org_id == subject.org_id,
            WorkflowDefinition.status == WorkflowDefinitionStatus.ACTIVE.value,
            access,
        ]
        if auto_routable_only:
            conditions.append(WorkflowDefinition.auto_routable.is_(True))
        result = await session.execute(
            select(WorkflowDefinition)
            .where(*conditions)
            .order_by(WorkflowDefinition.name, WorkflowDefinition.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def load_graph(
        self,
        session: AsyncSession,
        org_id: UUID,
        workflow_id: UUID,
    ) -> tuple[list[WorkflowNode], list[WorkflowEdge]]:
        nodes = list(
            (
                await session.execute(
                    select(WorkflowNode)
                    .where(
                        WorkflowNode.org_id == org_id,
                        WorkflowNode.workflow_definition_id == workflow_id,
                    )
                    .order_by(WorkflowNode.node_key)
                )
            ).scalars()
        )
        edges = list(
            (
                await session.execute(
                    select(WorkflowEdge)
                    .where(
                        WorkflowEdge.org_id == org_id,
                        WorkflowEdge.workflow_definition_id == workflow_id,
                    )
                    .order_by(WorkflowEdge.source_node_id, WorkflowEdge.target_node_id)
                )
            ).scalars()
        )
        return nodes, edges

    async def is_agent_referenced(
        self,
        session: AsyncSession,
        org_id: UUID,
        agent_id: UUID,
    ) -> bool:
        definition_reference = await session.scalar(
            select(WorkflowDefinition.id)
            .where(
                WorkflowDefinition.org_id == org_id,
                WorkflowDefinition.supervisor_agent_definition_id == agent_id,
            )
            .limit(1)
        )
        if definition_reference is not None:
            return True
        node_reference = await session.scalar(
            select(WorkflowNode.id)
            .where(
                WorkflowNode.org_id == org_id,
                WorkflowNode.agent_definition_id == agent_id,
            )
            .limit(1)
        )
        return node_reference is not None

    async def _insert_graph(
        self,
        session: AsyncSession,
        org_id: UUID,
        workflow_id: UUID,
        data: WorkflowDefinitionCreate | WorkflowDefinitionReplace,
    ) -> None:
        nodes_by_key: dict[str, WorkflowNode] = {}
        for item in data.nodes:
            node = WorkflowNode(
                org_id=org_id,
                workflow_definition_id=workflow_id,
                node_key=item.node_key,
                display_name=item.display_name,
                agent_definition_id=item.agent_definition_id,
                instruction=item.instruction,
                position_x=item.position_x,
                position_y=item.position_y,
                requires_confirmation=item.requires_confirmation,
            )
            session.add(node)
            nodes_by_key[item.node_key] = node
        await session.flush()
        session.add_all(
            [
                WorkflowEdge(
                    org_id=org_id,
                    workflow_definition_id=workflow_id,
                    source_node_id=nodes_by_key[item.source_node_key].id,
                    target_node_id=nodes_by_key[item.target_node_key].id,
                    condition=item.condition,
                    is_loop_back=item.is_loop_back,
                    max_iterations=item.max_iterations,
                )
                for item in data.edges
            ]
        )


workflow_definition_repository = WorkflowDefinitionRepository()
