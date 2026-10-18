import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_dependencies import authorization_subject
from src.core.dependencies import get_org_db_session
from src.schemas.common import ApiResponse, Meta
from src.schemas.interaction import QuestionResponse, SubmitAnswerRequest
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


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, InteractionNotFound):
        return HTTPException(status_code=404, detail="Question not found")
    if isinstance(exc, InteractionAccessDenied):
        return HTTPException(status_code=403, detail="Interaction access revoked")
    return HTTPException(status_code=409, detail="Interaction state conflict")


@router.get("/conversations/{conversation_id}/questions")
async def list_questions(
    conversation_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[QuestionResponse]]:
    try:
        questions = await interaction_service.list_questions(
            session, authorization_subject(user), conversation_id
        )
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        raise _translate(exc) from exc
    return ApiResponse(data=questions, meta=_meta())


@router.post("/conversations/{conversation_id}/questions/{question_id}/answer")
async def answer_question(
    conversation_id: UUID,
    question_id: UUID,
    data: SubmitAnswerRequest,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[QuestionResponse]:
    try:
        question = await interaction_service.answer_question(
            session,
            authorization_subject(user),
            conversation_id,
            question_id,
            data.idempotency_key,
            data.answer,
        )
        await session.commit()
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        await session.rollback()
        raise _translate(exc) from exc
    return ApiResponse(data=question, meta=_meta())


@router.post(
    "/conversations/{conversation_id}/turns/{turn_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_turn(
    conversation_id: UUID,
    turn_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    try:
        targets = await interaction_service.cancel_turn(
            session, authorization_subject(user), conversation_id, turn_id
        )
        await session.commit()
    except (InteractionNotFound, InteractionAccessDenied, InteractionConflict) as exc:
        await session.rollback()
        raise _translate(exc) from exc
    await interaction_service.signal_turn_cancel(targets, turn_id, conversation_id)
