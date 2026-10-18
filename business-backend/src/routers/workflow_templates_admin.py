import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import authorization_subject, require_org_admin
from src.core.dependencies import get_org_db_session
from src.core.exceptions import PlatformException
from src.repositories.workflow_definition_repository import workflow_definition_repository
from src.repositories.workflow_template_repository import WorkflowTemplateRepository
from src.schemas.common import ApiResponse, Meta
from src.schemas.workflow_template import (
    TemplateCaptureRequest,
    TemplateInstantiateRequest,
    TemplateInstantiationResponse,
    TemplateListItem,
    TemplateReplaceRequest,
    TemplateResponse,
    TemplateVersionRequest,
)
from src.services.workflow_graph import WorkflowGraphError
from src.services.workflow_presentation import workflow_definition_response
from src.services.workflow_template import WorkflowTemplateService
from src.services.workflow_template_errors import WorkflowTemplateError


class TemplateRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                raise PlatformException(
                    "snapshot_invalid", "Invalid template request", 422
                ) from exc

        return handle


router = APIRouter(prefix="/workflow-templates", route_class=TemplateRoute)
repository = WorkflowTemplateRepository()


def get_workflow_template_service() -> WorkflowTemplateService:
    return WorkflowTemplateService()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(UTC).isoformat(),
    )


def _is_name_conflict(exc: IntegrityError) -> bool:
    # SQLAlchemy's asyncpg adapter keeps the actual constraint on its cause.
    # Never classify by SQL/error text: unrelated constraints are internal errors.
    current: BaseException | None = exc.orig
    while current is not None:
        if getattr(current, "constraint_name", None) in {
            "uq_workflow_templates_org_name",
            "uq_workflow_definitions_org_name",
        }:
            return True
        current = current.__cause__
    return False


@asynccontextmanager
async def _operation(session: AsyncSession, *, commit: bool = False) -> AsyncIterator[None]:
    """Validate the complete response inside this boundary, before committing."""
    try:
        yield
        if commit:
            await session.commit()
    except WorkflowTemplateError as exc:
        await session.rollback()
        raise PlatformException(
            exc.code, "Workflow template operation failed", exc.status_code
        ) from exc
    except (ValidationError, WorkflowGraphError) as exc:
        await session.rollback()
        raise PlatformException("snapshot_invalid", "Invalid template snapshot", 422) from exc
    except IntegrityError as exc:
        await session.rollback()
        if _is_name_conflict(exc):
            raise PlatformException(
                "name_conflict", "Workflow template name conflict", 409
            ) from exc
        # The global IntegrityError handler maps every constraint to 409. Wrap
        # unknown constraints explicitly to preserve the internal-error boundary.
        raise PlatformException("INTERNAL_ERROR", "Internal server error", 500) from exc
    except Exception:
        await session.rollback()
        raise


@router.get("")
async def list_templates(
    include_archived: bool = Query(default=False, alias="includeArchived"),
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[TemplateListItem]]:
    async with _operation(session):
        items = await repository.list_by_org(
            session,
            authorization_subject(user).org_id,
            include_archived=include_archived,
        )
        result = ApiResponse(
            data=[TemplateListItem.model_validate(item, from_attributes=True) for item in items],
            meta=_meta(),
        )
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def capture_template(
    data: TemplateCaptureRequest,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowTemplateService = Depends(get_workflow_template_service),
) -> ApiResponse[TemplateResponse]:
    async with _operation(session, commit=True):
        template = await service.capture(session, authorization_subject(user), data)
        result = ApiResponse(
            data=TemplateResponse.model_validate(template, from_attributes=True), meta=_meta()
        )
    return result


@router.get("/{template_id}")
async def get_template(
    template_id: UUID,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[TemplateResponse]:
    async with _operation(session):
        template = await repository.get(session, authorization_subject(user).org_id, template_id)
        if template is None:
            raise WorkflowTemplateError("template_not_found", 404)
        result = ApiResponse(
            data=TemplateResponse.model_validate(template, from_attributes=True), meta=_meta()
        )
    return result


@router.put("/{template_id}")
async def replace_template(
    template_id: UUID,
    data: TemplateReplaceRequest,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowTemplateService = Depends(get_workflow_template_service),
) -> ApiResponse[TemplateResponse]:
    async with _operation(session, commit=True):
        template = await service.replace(session, authorization_subject(user), template_id, data)
        result = ApiResponse(
            data=TemplateResponse.model_validate(template, from_attributes=True), meta=_meta()
        )
    return result


@router.post("/{template_id}/instantiate", status_code=status.HTTP_201_CREATED)
async def instantiate_template(
    template_id: UUID,
    data: TemplateInstantiateRequest,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowTemplateService = Depends(get_workflow_template_service),
) -> ApiResponse[TemplateInstantiationResponse]:
    actor = authorization_subject(user)
    async with _operation(session, commit=True):
        created = await service.instantiate(session, actor, template_id, data)
        await session.refresh(created.workflow)
        nodes, edges = await workflow_definition_repository.load_graph(
            session, actor.org_id, created.workflow.id
        )
        result = ApiResponse(
            data=TemplateInstantiationResponse(
                workflow=workflow_definition_response(created.workflow, nodes, edges),
                agent_ids=created.agent_ids,
            ),
            meta=_meta(),
        )
    return result


@router.post("/{template_id}/archive")
async def archive_template(
    template_id: UUID,
    data: TemplateVersionRequest,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowTemplateService = Depends(get_workflow_template_service),
) -> ApiResponse[TemplateResponse]:
    async with _operation(session, commit=True):
        template = await service.archive(session, authorization_subject(user), template_id, data)
        result = ApiResponse(
            data=TemplateResponse.model_validate(template, from_attributes=True), meta=_meta()
        )
    return result
