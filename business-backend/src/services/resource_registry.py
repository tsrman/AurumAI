from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import ResourceRef, ResourceType
from src.core.logging import logger
from src.models.skill import Skill
from src.models.knowledge_base import KnowledgeBase
from src.models.agent_definition import AgentDefinition
from src.models.workflow_definition import WorkflowDefinition
from src.services.model_catalog import ModelCatalog, gateway_models


class ResourceRegistry:
    def __init__(self, *, model_catalog: ModelCatalog = gateway_models) -> None:
        self._model_catalog = model_catalog

    async def exists(
        self,
        session: AsyncSession,
        org_id: UUID,
        resource: ResourceRef,
    ) -> bool:
        if resource.resource_type is ResourceType.SKILL:
            try:
                skill_id = UUID(resource.resource_id)
            except ValueError:
                return False
            statement = select(Skill.id).where(
                Skill.id == skill_id,
                or_(Skill.org_id == org_id, Skill.org_id.is_(None)),
            )
            return (await session.execute(statement)).scalar_one_or_none() is not None
        if resource.resource_type is ResourceType.MODEL:
            try:
                models = await self._model_catalog()
            except Exception:
                logger.warning("authorization_resource_catalog_unavailable", exc_info=True)
                return False
            return any(
                isinstance(model, dict)
                and str(model.get("id") or "") == resource.resource_id
                for model in models
            )
        if resource.resource_type is ResourceType.KNOWLEDGE_BASE:
            try:
                knowledge_base_id = UUID(resource.resource_id)
            except ValueError:
                return False
            statement = select(KnowledgeBase.id).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.org_id == org_id,
            )
            return (await session.execute(statement)).scalar_one_or_none() is not None
        if resource.resource_type is ResourceType.AGENT_DEFINITION:
            try:
                agent_id = UUID(resource.resource_id)
            except ValueError:
                return False
            statement = select(AgentDefinition.id).where(
                AgentDefinition.id == agent_id,
                AgentDefinition.org_id == org_id,
            )
            return (await session.execute(statement)).scalar_one_or_none() is not None
        if resource.resource_type is ResourceType.WORKFLOW:
            try:
                workflow_id = UUID(resource.resource_id)
            except ValueError:
                return False
            statement = select(WorkflowDefinition.id).where(
                WorkflowDefinition.id == workflow_id,
                WorkflowDefinition.org_id == org_id,
            )
            return (await session.execute(statement)).scalar_one_or_none() is not None
        return False


resource_registry = ResourceRegistry()
