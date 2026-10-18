import logging
import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_dependencies import authorization_subject
from src.core.dependencies import get_org_db_session
from src.models.workflow_definition import WorkflowDefinition, WorkflowDefinitionStatus
from src.repositories.workflow_definition_repository import workflow_definition_repository
from src.repositories.workflow_run_repository import WorkflowTransitionRejected
from src.schemas.common import ApiResponse, Meta
from src.schemas.workflow import (
    WorkflowCancelResponse,
    WorkflowDecisionRequest,
    WorkflowDecisionResponse,
    WorkflowListItem,
    WorkflowRunSafeResponse,
)
from src.services.workflow_authorization import (
    WorkflowDefinitionNotReadable,
    WorkflowStepAccessDenied,
    workflow_authorization_service,
)
from src.services.workflow_run import WorkflowRunNotFound, workflow_run_service
from src.core.temporal import get_temporal_client

logger = logging.getLogger(__name__)


router = APIRouter()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


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


@router.get("/workflows")
async def list_workflows(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[WorkflowListItem]]:
    definitions = await workflow_definition_repository.list_accessible(
        session, authorization_subject(user)
    )
    return ApiResponse(data=[_list_item(item) for item in definitions], meta=_meta())


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[WorkflowListItem]:
    try:
        definition = await workflow_authorization_service.require_definition_readable(
            session, authorization_subject(user), workflow_id
        )
    except WorkflowDefinitionNotReadable as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc
    return ApiResponse(data=_list_item(definition), meta=_meta())


@router.get("/workflow-runs/{run_id}")
async def get_workflow_run(
    run_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[WorkflowRunSafeResponse]:
    try:
        run = await workflow_run_service.get_safe_run(
            session, authorization_subject(user), run_id
        )
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail="Workflow run not found") from exc
    return ApiResponse(data=run, meta=_meta())


@router.post("/workflow-runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_workflow_run(
    run_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[WorkflowCancelResponse]:
    try:
        run = await workflow_run_service.request_cancel(
            session, authorization_subject(user), run_id
        )
        await session.commit()
        handle = get_temporal_client().get_workflow_handle(
            f"conversation-{run.conversation_id}"
        )
        await handle.signal("cancel_workflow_run", str(run.id))
    except WorkflowRunNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Workflow run not found") from exc
    except WorkflowTransitionRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Workflow run is not active") from exc
    except HTTPException:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        logger.warning(
            "workflow_cancel_unavailable", exc_info=True, extra={"run_id": str(run_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow cancellation unavailable",
        ) from exc
    return ApiResponse(data=WorkflowCancelResponse(run_id=run_id), meta=_meta())


@router.post(
    "/workflow-runs/{run_id}/nodes/{node_key}/decision",
    status_code=status.HTTP_202_ACCEPTED,
)
async def decide_workflow_node(
    run_id: UUID,
    node_key: str,
    data: WorkflowDecisionRequest,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[WorkflowDecisionResponse]:
    subject = authorization_subject(user)
    try:
        run = await workflow_run_service.request_decision(
            session, subject, run_id, node_key
        )
        # Live re-check immediately before acting on the decision — same
        # invariant every node execution already gets (authorize_workflow_step),
        # so a grant revoked while the run sat waiting blocks the approval too,
        # not just the eventual execution.
        await workflow_authorization_service.require_snapshot_step_executable(
            session, subject, run, phase="node", node_key=node_key
        )
        handle = get_temporal_client().get_workflow_handle(f"{run.id}:orchestrator")
        await handle.signal(
            "submit_human_decision", args=[node_key, data.decision]
        )
    except WorkflowRunNotFound as exc:
        raise HTTPException(status_code=404, detail="Workflow run not found") from exc
    except WorkflowStepAccessDenied as exc:
        raise HTTPException(
            status_code=403, detail="Node dependencies are no longer accessible"
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "workflow_decision_unavailable",
            exc_info=True,
            extra={"run_id": str(run_id), "node_key": node_key},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow decision unavailable",
        ) from exc
    return ApiResponse(
        data=WorkflowDecisionResponse(run_id=run_id, node_key=node_key), meta=_meta()
    )
