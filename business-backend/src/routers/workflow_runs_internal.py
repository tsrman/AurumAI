import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.authorization_dependencies import authorization_subject
from src.core.dependencies import get_service_db_session
from src.models.workflow_run import WorkflowRun
from src.repositories.workflow_run_repository import (
    WorkflowNodeTransitionRejected,
    WorkflowOutputLimitExceeded,
    WorkflowTransitionRejected,
    workflow_run_repository,
)
from src.schemas.common import ApiResponse, Meta
from src.schemas.workflow import (
    WorkflowAuthorizeStepRequest,
    WorkflowNodeResultRequest,
    WorkflowRunSnapshotResponse,
    WorkflowStepDecisionResponse,
    WorkflowTransitionRequest,
)
from src.services.session_manager import session_manager
from src.services.workflow_authorization import (
    WorkflowStepAccessDenied,
    workflow_authorization_service,
)
from src.services.workflow_run import workflow_run_service


router = APIRouter(prefix="/workflow-runs")


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Workflow run not found")


async def _session_run(
    session: AsyncSession,
    user: UserInfo,
    run_id: UUID,
) -> WorkflowRun:
    if user.raw_claims.get("type") != "session":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only runner session tokens can access workflow runs",
        )
    try:
        org_id = UUID(user.org_id or "")
        user_id = UUID(user.user_id)
        conversation_id = UUID(str(user.raw_claims.get("conversation_id", "")))
    except ValueError as exc:
        raise _not_found() from exc
    run = await workflow_run_repository.get_by_id(session, run_id, org_id=org_id)
    if (
        run is None
        or run.initiating_user_id != user_id
        or run.conversation_id != conversation_id
    ):
        raise _not_found()
    return run


@router.get("/{run_id}/snapshot")
async def get_snapshot(
    run_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[WorkflowRunSnapshotResponse]:
    run = await _session_run(session, user, run_id)
    try:
        snapshot = WorkflowRunSnapshotResponse.model_validate(run.execution_snapshot)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail="Invalid workflow snapshot") from exc
    return ApiResponse(data=snapshot, meta=_meta())


@router.post("/{run_id}/touch-activity", status_code=status.HTTP_204_NO_CONTENT)
async def touch_activity(
    run_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> None:
    """Refreshes this conversation's runner idle timestamp (and session
    token, if stale) — called once when a node enters waiting_human.
    RUNNER_IDLE_REAP_SECONDS defaults to the same 1800s as the confirmation
    timeout, so without this a runner idle for a while before the pause
    could be reaped mid-wait, stranding the pending signal until an
    unrelated new chat message happens to recreate a runner."""
    run = await _session_run(session, user, run_id)
    await session_manager.get_or_create(
        str(run.conversation_id), user.user_id, user.org_id
    )


@router.post("/{run_id}/authorize-step")
async def authorize_step(
    run_id: UUID,
    data: WorkflowAuthorizeStepRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[WorkflowStepDecisionResponse]:
    run = await _session_run(session, user, run_id)
    try:
        decision = await workflow_authorization_service.require_snapshot_step_executable(
            session,
            authorization_subject(user),
            run,
            phase=data.phase,
            node_key=data.node_key,
        )
    except WorkflowStepAccessDenied as exc:
        raise _not_found() from exc
    return ApiResponse(
        data=WorkflowStepDecisionResponse(
            run_id=decision.run_id,
            phase=decision.phase,
            node_key=decision.node_key,
        ),
        meta=_meta(),
    )


@router.post("/{run_id}/transition")
async def transition_run(
    run_id: UUID,
    data: WorkflowTransitionRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[dict[str, str]]:
    await _session_run(session, user, run_id)
    try:
        run = await workflow_run_service.transition(
            session, run_id, data.status, data.error_code
        )
        await session.commit()
    except WorkflowTransitionRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Workflow transition rejected") from exc
    return ApiResponse(data={"status": run.status}, meta=_meta())


@router.put("/{run_id}/nodes/{phase_key}")
async def record_node_result(
    run_id: UUID,
    data: WorkflowNodeResultRequest,
    phase_key: str = Path(pattern=r"^[a-z][a-z0-9_-]{0,63}$"),
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[dict[str, str]]:
    await _session_run(session, user, run_id)
    try:
        await workflow_run_service.record_node_result(
            session,
            run_id,
            phase_key,
            data.status,
            data.output,
            data.error_code,
        )
        await session.commit()
    except WorkflowOutputLimitExceeded as exc:
        await session.rollback()
        raise HTTPException(status_code=413, detail=exc.code) from exc
    except WorkflowNodeTransitionRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Workflow node transition rejected") from exc
    except LookupError as exc:
        await session.rollback()
        raise _not_found() from exc
    return ApiResponse(data={"status": data.status}, meta=_meta())
