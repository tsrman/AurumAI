from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.models.workflow_template import WorkflowTemplate, WorkflowTemplateStatus
from src.schemas.workflow_template import TemplateSnapshot
from src.services.workflow_template_errors import WorkflowTemplateError


class WorkflowTemplateRepository:
    async def get(
        self,
        session: AsyncSession,
        org_id: UUID,
        template_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkflowTemplate | None:
        statement = (
            select(WorkflowTemplate)
            .where(
                WorkflowTemplate.org_id == org_id,
                WorkflowTemplate.id == template_id,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def list_by_org(
        self,
        session: AsyncSession,
        org_id: UUID,
        *,
        include_archived: bool = False,
    ) -> list[WorkflowTemplate]:
        statement = select(WorkflowTemplate).where(
            WorkflowTemplate.org_id == org_id
        )
        if not include_archived:
            statement = statement.where(
                WorkflowTemplate.status == WorkflowTemplateStatus.ACTIVE.value
            )
        result = await session.execute(
            statement.order_by(WorkflowTemplate.name, WorkflowTemplate.id)
        )
        return list(result.scalars())

    async def create(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        *,
        name: str,
        description: str | None,
        snapshot: TemplateSnapshot,
    ) -> WorkflowTemplate:
        template = WorkflowTemplate(
            org_id=actor.org_id,
            name=name,
            description=description,
            snapshot=snapshot.model_dump(mode="json", by_alias=True),
            created_by=actor.user_id,
            updated_by=actor.user_id,
        )
        session.add(template)
        await session.flush()
        await session.refresh(template)
        return template

    async def replace_locked(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        template: WorkflowTemplate,
        *,
        version: int,
        name: str,
        description: str | None,
        snapshot: TemplateSnapshot,
    ) -> WorkflowTemplate:
        if template.version != version:
            raise WorkflowTemplateError("version_conflict", 409)
        if template.status == WorkflowTemplateStatus.ARCHIVED.value:
            raise WorkflowTemplateError("template_archived", 409)

        template.name = name
        template.description = description
        template.snapshot = snapshot.model_dump(mode="json", by_alias=True)
        template.version += 1
        template.updated_by = actor.user_id
        await session.flush()
        await session.refresh(template)
        return template

    async def archive_locked(
        self,
        session: AsyncSession,
        actor: AuthorizationSubject,
        template: WorkflowTemplate,
        *,
        version: int,
    ) -> WorkflowTemplate:
        if template.version != version:
            raise WorkflowTemplateError("version_conflict", 409)
        if template.status == WorkflowTemplateStatus.ARCHIVED.value:
            return template

        template.status = WorkflowTemplateStatus.ARCHIVED.value
        template.version += 1
        template.updated_by = actor.user_id
        await session.flush()
        await session.refresh(template)
        return template
