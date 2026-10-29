import uuid
from datetime import datetime, timezone
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.authorization_dependencies import authorization_subject
from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.core.dependencies import get_service_db_session
from src.models.interaction import InteractionExecution
from src.repositories.message_repository import message_repository
from src.schemas.common import ApiResponse, Meta
from src.schemas.interaction import (
    AnswerRequest,
    ExecutionPhase,
    ExecutionRegistrationRequest,
    ExecutionResponse,
    QuestionCreateRequest,
    QuestionResponse,
)
from src.services.interaction import (
    InteractionAccessDenied,
    InteractionConflict,
    InteractionNotFound,
    interaction_service,
)


router = APIRouter()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _session_identity(user: UserInfo) -> tuple[AuthorizationSubject, UUID]:
    if user.raw_claims.get("type") != "session":
        raise HTTPException(status_code=403, detail="Only runner session tokens are allowed")
    try:
        conversation_id = UUID(str(user.raw_claims.get("conversation_id", "")))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid session identity") from exc
    return authorization_subject(user), conversation_id


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, InteractionNotFound):
        return HTTPException(status_code=404, detail="Interaction not found")
    if isinstance(exc, InteractionAccessDenied):
        return HTTPException(status_code=403, detail="Interaction access revoked")
    if isinstance(exc, InteractionConflict):
        return HTTPException(status_code=409, detail={"code": exc.code})
    return HTTPException(status_code=409, detail="Interaction state conflict")


def _execution_response(row: InteractionExecution) -> ExecutionResponse:
    return ExecutionResponse(
        execution_id=row.id,
        turn_id=row.turn_id,
        workflow_id=row.workflow_id,
        run_id=row.run_id,
        task_queue=row.task_queue,
        phase=cast(ExecutionPhase, row.phase),
        node_key=row.node_key,
        agent_definition_id=row.agent_definition_id,
        iteration=row.iteration,
    )


class TurnFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # What the user is told; kept as the turn's answer, marked failed, so the
    # history shows it and the runner never sends it back to a model.
    reason: str | None = Field(default=None, min_length=1, max_length=4000)


@router.post("/turns/{turn_id}/fail", status_code=status.HTTP_204_NO_CONTENT)
async def fail_turn(
    turn_id: UUID,
    data: TurnFailure | None = Body(default=None),
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> None:
    subject, conversation_id = _session_identity(user)
    try:
        await interaction_service.finish_turn(
            session,
            subject.org_id,
            subject.user_id,
            conversation_id,
            turn_id,
            status="failed",
        )
        if data is not None and data.reason:
            await message_repository.upsert_assistant_by_turn(
                session,
                conversation_id=str(conversation_id),
                turn_id=turn_id,
                role="assistant",
                content=data.reason,
                org_id=str(subject.org_id),
                extra={"failed": True},
            )
        await session.commit()
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        await session.rollback()
        raise _translate(exc) from exc


@router.post("/turns/{turn_id}/executions", status_code=status.HTTP_201_CREATED)
async def register_execution(
    turn_id: UUID,
    data: ExecutionRegistrationRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[ExecutionResponse]:
    subject, conversation_id = _session_identity(user)
    try:
        row = await interaction_service.register_execution(
            session, subject, conversation_id, turn_id, data
        )
        await session.commit()
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        await session.rollback()
        raise _translate(exc) from exc
    return ApiResponse(data=_execution_response(row), meta=_meta())


@router.post("/executions/{execution_id}/questions", status_code=status.HTTP_201_CREATED)
async def create_question(
    execution_id: UUID,
    data: QuestionCreateRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[QuestionResponse]:
    subject, conversation_id = _session_identity(user)
    try:
        row = await interaction_service.create_question(
            session,
            subject,
            conversation_id,
            execution_id,
            data,
            allow_new=settings.USER_QUESTIONS_ENABLED,
        )
        await session.commit()
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        await session.rollback()
        raise _translate(exc) from exc
    return ApiResponse(data=row, meta=_meta())


@router.get("/questions/{question_id}")
async def get_question(
    question_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[QuestionResponse]:
    subject, conversation_id = _session_identity(user)
    try:
        row = await interaction_service.get_question(session, subject, conversation_id, question_id)
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        raise _translate(exc) from exc
    return ApiResponse(data=row, meta=_meta())


@router.post("/questions/{question_id}/consume")
async def consume_question(
    question_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[AnswerRequest]:
    subject, conversation_id = _session_identity(user)
    try:
        answer = await interaction_service.consume_question(
            session, subject, conversation_id, question_id
        )
        await session.commit()
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        await session.rollback()
        raise _translate(exc) from exc
    return ApiResponse(data=answer, meta=_meta())


@router.post("/questions/{question_id}/expire")
async def expire_question(
    question_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[QuestionResponse]:
    subject, conversation_id = _session_identity(user)
    try:
        row = await interaction_service.expire_question(
            session, subject, conversation_id, question_id
        )
        await session.commit()
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        await session.rollback()
        raise _translate(exc) from exc
    return ApiResponse(data=row, meta=_meta())
