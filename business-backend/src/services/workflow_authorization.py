from dataclasses import dataclass
from typing import Literal, cast
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
from src.models.workflow_definition import WorkflowDefinition, WorkflowDefinitionStatus
from src.models.workflow_run import WorkflowRun
from src.services.authorization import (
    AuthorizationService,
    ResourceAccessDenied,
    authorization_service,
)
from src.services.resource_registry import ResourceRegistry, resource_registry


WorkflowPhase = Literal["supervisor_prepare", "node", "supervisor_synthesize"]


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionNotReadable(Exception):
    workflow_id: UUID


@dataclass(frozen=True, slots=True)
class WorkflowStepAccessDenied(Exception):
    run_id: UUID


@dataclass(frozen=True, slots=True)
class WorkflowStepDecision:
    run_id: UUID
    phase: WorkflowPhase
    node_key: str | None


class WorkflowAuthorizationService:
    def __init__(
        self,
        *,
        authorization: AuthorizationService = authorization_service,
        registry: ResourceRegistry = resource_registry,
    ) -> None:
        self._authorization = authorization
        self._registry = registry

    async def require_definition_readable(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        workflow_id: UUID,
    ) -> WorkflowDefinition:
        definition = await session.scalar(
            select(WorkflowDefinition).where(
                WorkflowDefinition.id == workflow_id,
                WorkflowDefinition.org_id == subject.org_id,
                WorkflowDefinition.status == WorkflowDefinitionStatus.ACTIVE.value,
            )
        )
        if definition is None:
            raise WorkflowDefinitionNotReadable(workflow_id)
        try:
            await self._authorization.require_access(
                session,
                subject,
                ResourceRef(ResourceType.WORKFLOW, str(definition.id)),
                AccessPermission.READ,
                resource_org_id=definition.org_id,
            )
        except ResourceAccessDenied as exc:
            raise WorkflowDefinitionNotReadable(workflow_id) from exc
        return definition

    async def require_snapshot_step_executable(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        run: WorkflowRun,
        *,
        phase: str,
        node_key: str | None,
    ) -> WorkflowStepDecision:
        if run.org_id != subject.org_id:
            raise WorkflowStepAccessDenied(run.id)
        if phase not in {"supervisor_prepare", "node", "supervisor_synthesize"}:
            raise WorkflowStepAccessDenied(run.id)
        typed_phase = cast(WorkflowPhase, phase)
        try:
            agents = self._phase_agents(run, typed_phase, node_key)
            definition = await session.scalar(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.id == run.workflow_definition_id,
                    WorkflowDefinition.org_id == subject.org_id,
                )
            )
            if definition is None:
                raise WorkflowStepAccessDenied(run.id)
            await self._require_resource(
                session,
                subject,
                ResourceType.WORKFLOW,
                str(definition.id),
                definition.org_id,
            )
            for agent in agents:
                await self._require_agent_snapshot(session, subject, run, agent)
        except (KeyError, TypeError, ValueError, ResourceAccessDenied) as exc:
            raise WorkflowStepAccessDenied(run.id) from exc
        return WorkflowStepDecision(run.id, typed_phase, node_key)

    @classmethod
    def _phase_agents(
        cls,
        run: WorkflowRun,
        phase: WorkflowPhase,
        node_key: str | None,
    ) -> list[dict[str, object]]:
        """Agents whose live access must still hold for this phase to run.

        Synthesis consumes every node's output, so it re-checks the node agents
        alongside the supervisor: a grant revoked after a node completed must
        stop its result being folded into the answer, not merely stop that node
        from being started again.
        """
        supervisor = cls._agent_snapshot(run, "supervisor_prepare", None)
        if phase != "supervisor_synthesize":
            return [cls._agent_snapshot(run, phase, node_key)]
        nodes = run.execution_snapshot["nodes"]
        if not isinstance(nodes, list):
            raise TypeError("nodes must be a list")
        agents = [supervisor]
        for item in sorted(
            nodes, key=lambda entry: str(entry.get("nodeKey", ""))
        ):
            if not isinstance(item, dict) or not isinstance(item.get("agent"), dict):
                raise KeyError("snapshot node missing")
            agents.append(cast(dict[str, object], item["agent"]))
        return agents

    @staticmethod
    def _agent_snapshot(
        run: WorkflowRun,
        phase: WorkflowPhase,
        node_key: str | None,
    ) -> dict[str, object]:
        snapshot = run.execution_snapshot
        if phase == "node":
            if node_key is None:
                raise KeyError("node key required")
            nodes = snapshot["nodes"]
            if not isinstance(nodes, list):
                raise TypeError("nodes must be a list")
            matches = [
                item
                for item in nodes
                if isinstance(item, dict) and item.get("nodeKey") == node_key
            ]
            if len(matches) != 1 or not isinstance(matches[0].get("agent"), dict):
                raise KeyError("snapshot node missing")
            return cast(dict[str, object], matches[0]["agent"])
        supervisor = snapshot["supervisor"]
        if not isinstance(supervisor, dict):
            raise TypeError("supervisor must be an object")
        return cast(dict[str, object], supervisor)

    async def _require_agent_snapshot(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        run: WorkflowRun,
        agent: dict[str, object],
    ) -> None:
        agent_id = UUID(str(agent["agentDefinitionId"]))
        model_id = str(agent["model"])
        current_agent = await session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.id == agent_id,
                AgentDefinition.org_id == subject.org_id,
            )
        )
        if current_agent is None:
            raise WorkflowStepAccessDenied(run.id)
        await self._require_resource(
            session,
            subject,
            ResourceType.AGENT_DEFINITION,
            str(agent_id),
            subject.org_id,
        )
        if not await self._registry.exists(
            session, subject.org_id, ResourceRef(ResourceType.MODEL, model_id)
        ):
            raise WorkflowStepAccessDenied(run.id)
        await self._require_resource(
            session, subject, ResourceType.MODEL, model_id, subject.org_id
        )

        raw_skill_ids = agent.get("allowedSkillIds", [])
        if not isinstance(raw_skill_ids, list):
            raise TypeError("allowed skill IDs must be a list")
        skill_ids = [UUID(str(item)) for item in raw_skill_ids]
        skill_rows = (
            await session.execute(
                select(Skill.id, Skill.org_id).where(
                    Skill.id.in_(skill_ids),
                    or_(Skill.org_id == subject.org_id, Skill.org_id.is_(None)),
                )
            )
        ).all()
        if {row.id for row in skill_rows} != set(skill_ids):
            raise WorkflowStepAccessDenied(run.id)
        skill_orgs = {row.id: row.org_id for row in skill_rows}
        for skill_id in skill_ids:
            await self._require_resource(
                session,
                subject,
                ResourceType.SKILL,
                str(skill_id),
                skill_orgs[skill_id],
            )

        raw_bases = agent.get("knowledgeBases", [])
        if not isinstance(raw_bases, list):
            raise TypeError("knowledge bases must be a list")
        base_ids = [UUID(str(item["id"])) for item in raw_bases if isinstance(item, dict)]
        if len(base_ids) != len(raw_bases):
            raise TypeError("knowledge base entry must be an object")
        existing_bases = set(
            (
                await session.execute(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.id.in_(base_ids),
                        KnowledgeBase.org_id == subject.org_id,
                    )
                )
            ).scalars()
        )
        if existing_bases != set(base_ids):
            raise WorkflowStepAccessDenied(run.id)
        for base_id in base_ids:
            await self._require_resource(
                session,
                subject,
                ResourceType.KNOWLEDGE_BASE,
                str(base_id),
                subject.org_id,
            )

    async def _require_resource(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        resource_type: ResourceType,
        resource_id: str,
        resource_org_id: UUID | None,
    ) -> None:
        await self._authorization.require_access(
            session,
            subject,
            ResourceRef(resource_type, resource_id),
            AccessPermission.READ,
            resource_org_id=resource_org_id,
        )


workflow_authorization_service = WorkflowAuthorizationService()
