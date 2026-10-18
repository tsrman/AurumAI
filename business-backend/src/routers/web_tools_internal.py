"""Runner-only web tool boundary with live auth and durable execution."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.authorization_dependencies import authorization_subject
from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.core.dependencies import get_service_db_session
from src.schemas.web_tools import WebErrorCode, WebOperationRequest, WebToolError
from src.services.interaction import (
    InteractionAccessDenied,
    InteractionConflict,
    InteractionNotFound,
    interaction_service,
)
from src.services.web_tools import WebToolExecutionError, web_tools_service


router = APIRouter()

_WEB_ERROR_CODES = {
    "web_disabled",
    "web_access_denied",
    "web_limit",
    "web_timeout",
    "web_unavailable",
    "web_url_blocked",
    "web_too_large",
    "unsupported_document",
    "web_cancelled",
}


def _session_identity(user: UserInfo) -> tuple[AuthorizationSubject, UUID]:
    if user.raw_claims.get("type") != "session":
        raise HTTPException(status_code=403, detail="Only runner session tokens are allowed")
    try:
        conversation_id = UUID(str(user.raw_claims.get("conversation_id", "")))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid session identity") from exc
    return authorization_subject(user), conversation_id


def _error(code: WebErrorCode, status_code: int) -> HTTPException:
    payload = WebToolError(code=code)
    return HTTPException(status_code=status_code, detail=payload.model_dump())


@router.post(
    "/turns/{turn_id}/executions/{execution_id}/web-operations/{invocation_id}"
)
async def execute_web_operation(
    turn_id: UUID,
    execution_id: UUID,
    invocation_id: UUID,
    data: WebOperationRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> dict[str, object]:
    # Resolve identity even while disabled: browser credentials can never use
    # this internal surface, and malformed session identity stays a 401.
    subject, conversation_id = _session_identity(user)
    if not settings.WEB_ENABLED:
        raise _error("web_disabled", status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        await interaction_service.authorize_web_operation(
            session,
            subject,
            conversation_id,
            turn_id,
            execution_id,
        )
    except InteractionNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Interaction not found") from exc
    except InteractionAccessDenied as exc:
        await session.rollback()
        raise _error("web_access_denied", status.HTTP_403_FORBIDDEN) from exc
    except InteractionConflict as exc:
        await session.rollback()
        if exc.code == "user_input_cancelled":
            raise _error("web_cancelled", status.HTTP_409_CONFLICT) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code},
        ) from exc

    try:
        return await web_tools_service.execute(
            session,
            subject,
            execution_id,
            invocation_id,
            data.tool,
            data.args.model_dump(mode="json"),
        )
    except WebToolExecutionError as exc:
        await session.rollback()
        if exc.code == "interaction_not_found":
            raise HTTPException(status_code=404, detail="Interaction not found") from exc
        if exc.code == "interaction_conflict":
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
        code = exc.code if exc.code in _WEB_ERROR_CODES else "web_unavailable"
        status_code = {
            "web_access_denied": status.HTTP_403_FORBIDDEN,
            "web_limit": status.HTTP_429_TOO_MANY_REQUESTS,
            "web_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
            "web_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
            "web_url_blocked": status.HTTP_422_UNPROCESSABLE_CONTENT,
            "unsupported_document": status.HTTP_422_UNPROCESSABLE_CONTENT,
            "web_cancelled": status.HTTP_409_CONFLICT,
        }.get(code, status.HTTP_503_SERVICE_UNAVAILABLE)
        raise _error(cast(WebErrorCode, code), status_code) from exc
