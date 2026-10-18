import asyncio
import uuid
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceRef,
    ResourceType,
)
from src.core.config import settings
from src.core.database import async_session
from src.core.logging import logger
from src.models.agent_definition import AgentDefinition
from src.models.knowledge_base import KnowledgeBase
from src.models.message import Message
from src.models.organization_settings import OrganizationSettings
from src.services.model_parameters import resolve_temperature
from src.models.workflow_run import WorkflowNodeRun, WorkflowRun, WorkflowRunStatus
from src.models.workflow_definition import WorkflowDefinition, WorkflowDefinitionStatus
from src.repositories.workflow_definition_repository import (
    WorkflowDefinitionRepository,
    workflow_definition_repository,
)
from src.repositories.workflow_run_repository import (
    WorkflowRunRepository,
    workflow_run_repository,
)
from src.schemas.workflow import (
    WorkflowAgentSnapshot,
    WorkflowEdgeDraft,
    WorkflowKnowledgeBaseSnapshot,
    WorkflowNodeDraft,
    WorkflowNodeDiagnosticResponse,
    WorkflowNodeStatusResponse,
    WorkflowRunDiagnosticResponse,
    WorkflowRunSafeResponse,
    WorkflowRunSnapshotResponse,
    WorkflowSnapshotNode,
)
from src.services.agent_authorization import (
    AgentAuthorizationService,
    AgentDependencyAccessDenied,
    agent_authorization_service,
)
from src.services.authorization import (
    AuthorizationService,
    ResourceAccessDenied,
    authorization_service,
)
from src.services.workflow_authorization import (
    WorkflowAuthorizationService,
    WorkflowDefinitionNotReadable,
    workflow_authorization_service,
)
from src.services.workflow_graph import WorkflowGraphError, validate_workflow_graph
from src.services.interaction import (
    InteractionNotFound,
    InteractionService,
    interaction_service,
)


class WorkflowRunNotFound(Exception):
    pass


class WorkflowRunService:
    def __init__(
        self,
        *,
        repository: WorkflowRunRepository = workflow_run_repository,
        definitions: WorkflowDefinitionRepository = workflow_definition_repository,
        authorization: WorkflowAuthorizationService = workflow_authorization_service,
        resource_authorization: AuthorizationService = authorization_service,
        agent_authorization: AgentAuthorizationService = agent_authorization_service,
        session_factory: async_sessionmaker[AsyncSession] = async_session,
        interactions: InteractionService | None = None,
    ) -> None:
        self._repository = repository
        self._definitions = definitions
        self._authorization = authorization
        self._resource_authorization = resource_authorization
        self._agent_authorization = agent_authorization
        self._session_factory = session_factory
        self._interactions = interactions or interaction_service

    async def create_authorized_run(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        workflow_id: UUID,
        conversation_id: UUID,
        turn_id: UUID,
        message_id: UUID,
    ) -> WorkflowRun:
        definition = await self._authorization.require_definition_readable(
            session, subject, workflow_id
        )
        # Definition replacement/archive takes the same row lock.  Re-read after
        # acquiring it so the graph and all agent settings below form one coherent
        # immutable snapshot rather than a mix of two definition versions.
        locked_definition = await session.scalar(
            select(WorkflowDefinition)
            .where(
                WorkflowDefinition.id == definition.id,
                WorkflowDefinition.org_id == subject.org_id,
            )
            .with_for_update()
        )
        if (
            locked_definition is None
            or locked_definition.status != WorkflowDefinitionStatus.ACTIVE.value
        ):
            raise WorkflowDefinitionNotReadable(workflow_id)
        definition = locked_definition
        nodes, edges = await self._definitions.load_graph(session, subject.org_id, definition.id)
        node_keys = {node.id: node.node_key for node in nodes}
        # Stored rows are re-validated on every run.  A definition saved before
        # a rule existed (a node key colliding with a supervisor phase, say)
        # must surface as a clean "workflow unavailable" 409, so a schema
        # rejection here becomes a WorkflowGraphError rather than escaping as a
        # pydantic ValidationError and 500-ing the chat request.
        try:
            drafted_nodes = [
                WorkflowNodeDraft(
                    node_key=node.node_key,
                    display_name=node.display_name,
                    agent_definition_id=node.agent_definition_id,
                    instruction=node.instruction,
                    position_x=node.position_x,
                    position_y=node.position_y,
                    requires_confirmation=node.requires_confirmation,
                )
                for node in nodes
            ]
            drafted_edges = [
                WorkflowEdgeDraft(
                    source_node_key=node_keys[edge.source_node_id],
                    target_node_key=node_keys[edge.target_node_id],
                    condition=cast(Literal["on_success", "on_failure"], edge.condition),
                    is_loop_back=edge.is_loop_back,
                    max_iterations=edge.max_iterations,
                )
                for edge in edges
            ]
        except ValidationError as exc:
            raise WorkflowGraphError("invalid_definition") from exc
        graph = validate_workflow_graph(
            drafted_nodes,
            drafted_edges,
            max_nodes=settings.WORKFLOW_MAX_NODES,
        )
        if definition.max_parallelism > settings.WORKFLOW_MAX_PARALLELISM:
            raise WorkflowGraphError("parallelism_limit")
        message = await session.scalar(
            select(Message).where(
                Message.id == message_id,
                Message.org_id == subject.org_id,
                Message.conversation_id == conversation_id,
                Message.turn_id == turn_id,
                Message.user_id == subject.user_id,
                Message.role == "user",
            )
        )
        if message is None or not message.content:
            raise WorkflowRunNotFound()

        agent_ids = {
            definition.supervisor_agent_definition_id,
            *(node.agent_definition_id for node in nodes),
        }
        agents = list(
            (
                await session.execute(
                    select(AgentDefinition)
                    .where(
                        AgentDefinition.org_id == subject.org_id,
                        AgentDefinition.id.in_(agent_ids),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        agents_by_id = {agent.id: agent for agent in agents}
        if set(agents_by_id) != agent_ids:
            raise WorkflowDefinitionNotReadable(workflow_id)
        # Resolved once here and baked into the frozen execution_snapshot,
        # consistent with the rest of this snapshot being fixed at run-start
        # (e.g. requiresConfirmation) rather than re-read live per node.
        org_default_temperature = await session.scalar(
            select(OrganizationSettings.default_temperature).where(
                OrganizationSettings.org_id == subject.org_id
            )
        )
        snapshots: dict[UUID, WorkflowAgentSnapshot] = {}
        for agent_id in sorted(agent_ids, key=str):
            agent = agents_by_id[agent_id]
            try:
                await self._resource_authorization.require_access(
                    session,
                    subject,
                    ResourceRef(ResourceType.AGENT_DEFINITION, str(agent.id)),
                    AccessPermission.READ,
                    resource_org_id=agent.org_id,
                )
                await self._agent_authorization.require_dependencies(
                    session,
                    subject,
                    agent_id=agent.id,
                    model_id=agent.model_id,
                    skill_ids=agent.skill_ids,
                    knowledge_base_ids=agent.knowledge_base_ids,
                )
            except (ResourceAccessDenied, AgentDependencyAccessDenied) as exc:
                raise WorkflowDefinitionNotReadable(workflow_id) from exc
            bases = list(
                (
                    await session.execute(
                        select(KnowledgeBase).where(
                            KnowledgeBase.org_id == subject.org_id,
                            KnowledgeBase.id.in_(agent.knowledge_base_ids),
                        )
                    )
                ).scalars()
            )
            bases_by_id = {base.id: base for base in bases}
            snapshots[agent.id] = WorkflowAgentSnapshot(
                agent_definition_id=agent.id,
                name=agent.name,
                model=agent.model_id,
                system_prompt=agent.system_prompt,
                temperature=resolve_temperature(agent.temperature, org_default_temperature),
                allowed_skill_ids=tuple(agent.skill_ids),
                knowledge_bases=tuple(
                    WorkflowKnowledgeBaseSnapshot(id=base_id, name=bases_by_id[base_id].name)
                    for base_id in agent.knowledge_base_ids
                ),
            )

        run_id = uuid.uuid4()
        created_at = datetime.now(UTC)
        temporal_workflow_id = f"conversation-{conversation_id}:workflow:{turn_id}"
        snapshot = WorkflowRunSnapshotResponse(
            run_id=run_id,
            org_id=subject.org_id,
            workflow_definition_id=definition.id,
            workflow_definition_version=definition.version,
            conversation_id=conversation_id,
            turn_id=turn_id,
            source_message_id=message_id,
            temporal_workflow_id=temporal_workflow_id,
            user_request=message.content,
            max_parallelism=definition.max_parallelism,
            supervisor=snapshots[definition.supervisor_agent_definition_id],
            nodes=tuple(
                WorkflowSnapshotNode(
                    node_key=node.node_key,
                    display_name=node.display_name,
                    instruction=node.instruction,
                    agent=snapshots[node.agent_definition_id],
                    requires_confirmation=node.requires_confirmation,
                )
                for node in nodes
            ),
            edges=tuple(
                WorkflowEdgeDraft(
                    source_node_key=node_keys[edge.source_node_id],
                    target_node_key=node_keys[edge.target_node_id],
                    condition=cast(Literal["on_success", "on_failure"], edge.condition),
                    is_loop_back=edge.is_loop_back,
                    max_iterations=edge.max_iterations,
                )
                for edge in edges
            ),
            loop_bodies=graph.loop_bodies,
            created_at=created_at,
        )
        run = WorkflowRun(
            id=run_id,
            org_id=subject.org_id,
            workflow_definition_id=definition.id,
            workflow_definition_version=definition.version,
            initiating_user_id=subject.user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            source_message_id=message_id,
            temporal_workflow_id=temporal_workflow_id,
            execution_snapshot=snapshot.model_dump(mode="json", by_alias=True),
            status=WorkflowRunStatus.PENDING.value,
        )
        session.add(run)
        # The node rows use a composite FK and intentionally do not expose an ORM
        # relationship.  Flush the parent explicitly so SQLAlchemy cannot batch the
        # child INSERTs ahead of the workflow run.
        await session.flush()
        supervisor = snapshots[definition.supervisor_agent_definition_id]
        session.add_all(
            [
                WorkflowNodeRun(
                    org_id=subject.org_id,
                    workflow_run_id=run_id,
                    phase_key="supervisor_prepare",
                    agent_definition_id=supervisor.agent_definition_id,
                    agent_name=supervisor.name,
                ),
                *[
                    WorkflowNodeRun(
                        org_id=subject.org_id,
                        workflow_run_id=run_id,
                        phase_key=node.node_key,
                        agent_definition_id=node.agent_definition_id,
                        agent_name=node.display_name,
                    )
                    for node in nodes
                ],
                WorkflowNodeRun(
                    org_id=subject.org_id,
                    workflow_run_id=run_id,
                    phase_key="supervisor_synthesize",
                    agent_definition_id=supervisor.agent_definition_id,
                    agent_name=supervisor.name,
                ),
            ]
        )
        await session.flush()
        return run

    async def get_safe_run(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        run_id: UUID,
    ) -> WorkflowRunSafeResponse:
        run = await self._repository.get_by_id(
            session,
            run_id,
            org_id=subject.org_id,
            user_id=None if subject.is_org_admin else subject.user_id,
        )
        if run is None:
            raise WorkflowRunNotFound()
        nodes = await self._repository.list_node_runs(session, run.id)
        return self._safe_response(run, nodes)

    async def get_admin_diagnostic(
        self,
        session: AsyncSession,
        org_id: UUID,
        run_id: UUID,
    ) -> WorkflowRunDiagnosticResponse:
        run = await self._repository.get_by_id(session, run_id, org_id=org_id)
        if run is None:
            raise WorkflowRunNotFound()
        nodes = await self._repository.list_node_runs(session, run.id)
        safe = self._safe_response(run, nodes)
        now = datetime.now(UTC)
        diagnostics = tuple(
            WorkflowNodeDiagnosticResponse(
                **self._node_status(row).model_dump(),
                output=None
                if row.diagnostic_expires_at is not None and row.diagnostic_expires_at <= now
                else row.output,
                internal_error=None
                if row.diagnostic_expires_at is not None and row.diagnostic_expires_at <= now
                else row.internal_error,
                diagnostic_expires_at=row.diagnostic_expires_at,
                output_expired=row.diagnostic_expires_at is not None
                and row.diagnostic_expires_at <= now,
            )
            for row in nodes
        )
        return WorkflowRunDiagnosticResponse(
            **safe.model_dump(exclude={"nodes"}),
            internal_error=run.internal_error,
            nodes=diagnostics,
        )

    async def request_cancel(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        run_id: UUID,
    ) -> WorkflowRun:
        run = await self._repository.get_by_id(
            session, run_id, org_id=subject.org_id, user_id=subject.user_id
        )
        if run is None:
            raise WorkflowRunNotFound()
        return await self.transition(
            session,
            run.id,
            WorkflowRunStatus.CANCELLED,
            "user_cancelled",
        )

    async def request_decision(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        run_id: UUID,
        node_key: str,
    ) -> WorkflowRun:
        """Ownership + shape validation only — does NOT transition any
        status. The actual pending -> running/failed transition is reported
        by the runner (record_node_result) once MultiAgentWorkflow actually
        wakes up and acts on the decision; this method (and the caller's own
        live-authorization recheck) only gate whether the signal is sent at
        all.
        """
        run = await self._repository.get_by_id(
            session, run_id, org_id=subject.org_id, user_id=subject.user_id
        )
        if run is None:
            raise WorkflowRunNotFound()
        nodes = run.execution_snapshot.get("nodes", [])
        node = next((n for n in nodes if n.get("nodeKey") == node_key), None)
        if node is None or not node.get("requiresConfirmation"):
            raise WorkflowRunNotFound()
        return run

    async def transition(
        self,
        session: AsyncSession,
        run_id: UUID,
        status: WorkflowRunStatus,
        error_code: str | None,
    ) -> WorkflowRun:
        run = await self._repository.transition(session, run_id, status, error_code)
        if status in {
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }:
            try:
                await self._interactions.finish_turn(
                    session,
                    run.org_id,
                    run.initiating_user_id,
                    run.conversation_id,
                    run.turn_id,
                    status=status.value,
                )
            except InteractionNotFound:
                # A rolling deploy can finish workflow runs that predate the
                # interaction schema. The workflow transition remains valid.
                pass
        return run

    async def record_node_result(
        self,
        session: AsyncSession,
        run_id: UUID,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> WorkflowNodeRun:
        return await self._repository.record_node_result(
            session, run_id, phase_key, status, output, error_code
        )

    async def clear_expired_diagnostics(self, now: datetime) -> int:
        async with self._session_factory() as session:
            cleared = await self._repository.clear_expired_diagnostics(session, now)
            await session.commit()
            return cleared

    async def run_retention_reaper(self) -> None:
        while True:
            try:
                await self.clear_expired_diagnostics(datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("workflow_diagnostic_reaper_failed", exc_info=True)
            await asyncio.sleep(settings.WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS)

    @staticmethod
    def _node_status(row: WorkflowNodeRun) -> WorkflowNodeStatusResponse:
        return WorkflowNodeStatusResponse(
            phase_key=row.phase_key,
            agent_definition_id=row.agent_definition_id,
            agent_name=row.agent_name,
            status=row.status,
            attempt_count=row.attempt_count,
            error_code=row.error_code,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )

    def _safe_response(
        self,
        run: WorkflowRun,
        nodes: list[WorkflowNodeRun],
    ) -> WorkflowRunSafeResponse:
        return WorkflowRunSafeResponse(
            id=run.id,
            workflow_definition_id=run.workflow_definition_id,
            workflow_definition_version=run.workflow_definition_version,
            conversation_id=run.conversation_id,
            turn_id=run.turn_id,
            status=WorkflowRunStatus(run.status),
            error_code=run.error_code,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            nodes=tuple(self._node_status(row) for row in nodes),
        )


workflow_run_service = WorkflowRunService()
