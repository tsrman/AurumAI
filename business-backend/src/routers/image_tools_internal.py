"""Runner-only image tool boundary with live auth and durable execution."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.authorization_dependencies import authorization_subject
from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.core.dependencies import get_service_db_session
from src.schemas.image_tools import ImageErrorCode, ImageOperationRequest, ImageToolError
from src.services.image_tools import (
    IMAGE_ERROR_CODES,
    ImageToolExecutionError,
    image_tools_service,
)


router = APIRouter()

_STATUS = {
    "image_disabled": status.HTTP_503_SERVICE_UNAVAILABLE,
    "image_access_denied": status.HTTP_403_FORBIDDEN,
    "image_limit": status.HTTP_429_TOO_MANY_REQUESTS,
    "image_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
    "image_rejected": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "image_not_found": status.HTTP_404_NOT_FOUND,
    "image_cancelled": status.HTTP_409_CONFLICT,
    "image_quota_exceeded": status.HTTP_429_TOO_MANY_REQUESTS,
}


def _session_identity(user: UserInfo) -> tuple[AuthorizationSubject, UUID]:
    if user.raw_claims.get("type") != "session":
        raise HTTPException(status_code=403, detail="Only runner session tokens are allowed")
    try:
        conversation_id = UUID(str(user.raw_claims.get("conversation_id", "")))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid session identity") from exc
    return authorization_subject(user), conversation_id


def _error(code: ImageErrorCode) -> HTTPException:
    return HTTPException(
        status_code=_STATUS.get(code, status.HTTP_503_SERVICE_UNAVAILABLE),
        detail=ImageToolError(code=code).model_dump(),
    )


@router.post(
    "/turns/{turn_id}/executions/{execution_id}/image-operations/{invocation_id}"
)
async def execute_image_operation(
    turn_id: UUID,
    execution_id: UUID,
    invocation_id: UUID,
    data: ImageOperationRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> dict[str, object]:
    # Resolve identity even while disabled: browser credentials can never use
    # this internal surface, and malformed session identity stays a 401.
    subject, conversation_id = _session_identity(user)
    if not settings.IMAGES_ENABLED:
        raise _error("image_disabled")
    try:
        turn = await image_tools_service.turn_for_execution(session, subject.org_id, execution_id)
        if turn.id != turn_id or turn.conversation_id != conversation_id:
            raise ImageToolExecutionError("interaction_not_found")
        return await image_tools_service.execute(
            session,
            subject,
            execution_id,
            invocation_id,
            data.tool,
            data.args.model_dump(mode="json"),
        )
    except ImageToolExecutionError as exc:
        await session.rollback()
        if exc.code == "interaction_not_found":
            raise HTTPException(status_code=404, detail="Interaction not found") from exc
        if exc.code == "interaction_conflict":
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
        code = exc.code if exc.code in IMAGE_ERROR_CODES else "image_unavailable"
        raise _error(cast(ImageErrorCode, code)) from exc
