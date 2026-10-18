import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import authorization_subject, require_org_admin
from src.core.dependencies import get_org_db_session
from src.models.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from src.repositories.workflow_definition_repository import (
    WorkflowVersionConflict,
    workflow_definition_repository,
)
from src.schemas.common import ApiResponse, Meta
from src.schemas.workflow import (
    WorkflowDefinitionCreate,
    WorkflowDefinitionReplace,
    WorkflowDefinitionResponse,
    WorkflowListItem,
    WorkflowValidationResponse,
    WorkflowRunDiagnosticResponse,
)
from src.services.workflow_definition import (
    WorkflowDefinitionNotFound,
    WorkflowDefinitionService,
    WorkflowDependencyNotFound,
    workflow_definition_service,
)
from src.services.workflow_graph import WorkflowGraphError
from src.services.workflow_presentation import workflow_definition_response
from src.services.workflow_run import WorkflowRunNotFound, workflow_run_service


router = APIRouter(prefix="/workflows")
diagnostics_router = APIRouter(prefix="/workflow-runs")


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def get_workflow_definition_service() -> WorkflowDefinitionService:
    return workflow_definition_service


@diagnostics_router.get("/{run_id}")
async def get_workflow_run_diagnostic(
    run_id: UUID,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[WorkflowRunDiagnosticResponse]:
    subject = authorization_subject(user)
    try:
        diagnostic = await workflow_run_service.get_admin_diagnostic(
            session, subject.org_id, run_id
        )
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail="Workflow run not found") from exc
    return ApiResponse(data=diagnostic, meta=_meta())


def _list_item(definition: WorkflowDefinition) -> WorkflowListItem:
    return WorkflowListItem(
        id=definition.id,
        name=definition.name,
        description=definition.description,
        version=definition.version,
        max_parallelism=definition.max_parallelism,
        status=WorkflowDefinitionStatus(definition.status),
        auto_routable=definition.auto_routable,
    )


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowDefinitionNotFound):
        return HTTPException(status_code=404, detail="Workflow definition not found")
    if isinstance(exc, WorkflowDependencyNotFound):
        return HTTPException(
            status_code=404,
            detail="Workflow dependency not found or invalid",
        )
    if isinstance(exc, WorkflowVersionConflict):
        return HTTPException(status_code=409, detail="Workflow version conflict")
    if isinstance(exc, WorkflowGraphError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "nodeKeys": list(exc.node_keys)},
        )
    raise exc


@router.post("/validate")
async def validate_workflow(
    data: WorkflowDefinitionCreate,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> ApiResponse[WorkflowValidationResponse]:
    try:
        graph = await service.validate_draft(session, authorization_subject(user), data)
    except (WorkflowDependencyNotFound, WorkflowGraphError) as exc:
        raise _map_error(exc) from exc
    return ApiResponse(
        data=WorkflowValidationResponse(
            topological_order=graph.topological_order,
            predecessors=graph.predecessors,
            loop_bodies=graph.loop_bodies,
        ),
        meta=_meta(),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow(
    data: WorkflowDefinitionCreate,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> ApiResponse[WorkflowDefinitionResponse]:
    subject = authorization_subject(user)
    try:
        definition = await service.create(session, subject, data)
        await session.commit()
        await session.refresh(definition)
    except (WorkflowDependencyNotFound, WorkflowGraphError) as exc:
        await session.rollback()
        raise _map_error(exc) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Workflow definition name already exists",
        ) from exc
    nodes, edges = await workflow_definition_repository.load_graph(
        session, subject.org_id, definition.id
    )
    return ApiResponse(
        data=workflow_definition_response(definition, nodes, edges), meta=_meta()
    )


@router.get("")
async def list_workflows(
    include_archived: bool = Query(default=False, alias="includeArchived"),
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[WorkflowListItem]]:
    subject = authorization_subject(user)
    definitions = await workflow_definition_repository.list_by_org(
        session, subject.org_id, include_archived=include_archived
    )
    return ApiResponse(data=[_list_item(item) for item in definitions], meta=_meta())


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: UUID,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[WorkflowDefinitionResponse]:
    subject = authorization_subject(user)
    definition = await workflow_definition_repository.get_by_id(
        session, subject.org_id, workflow_id
    )
    if definition is None:
        raise HTTPException(status_code=404, detail="Workflow definition not found")
    nodes, edges = await workflow_definition_repository.load_graph(
        session, subject.org_id, workflow_id
    )
    return ApiResponse(
        data=workflow_definition_response(definition, nodes, edges), meta=_meta()
    )


@router.put("/{workflow_id}")
async def replace_workflow(
    workflow_id: UUID,
    data: WorkflowDefinitionReplace,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> ApiResponse[WorkflowDefinitionResponse]:
    subject = authorization_subject(user)
    try:
        definition = await service.replace(session, subject, workflow_id, data)
        await session.commit()
        await session.refresh(definition)
    except (
        WorkflowDefinitionNotFound,
        WorkflowDependencyNotFound,
        WorkflowGraphError,
        WorkflowVersionConflict,
    ) as exc:
        await session.rollback()
        raise _map_error(exc) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Workflow reference conflict") from exc
    nodes, edges = await workflow_definition_repository.load_graph(
        session, subject.org_id, definition.id
    )
    return ApiResponse(
        data=workflow_definition_response(definition, nodes, edges), meta=_meta()
    )


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_workflow(
    workflow_id: UUID,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> None:
    try:
        await service.archive(session, authorization_subject(user), workflow_id)
        await session.commit()
    except WorkflowDefinitionNotFound as exc:
        await session.rollback()
        raise _map_error(exc) from exc
