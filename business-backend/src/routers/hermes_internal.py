"""Runner-only management of Hermes sessions (docs/HERMES_AGENT.md).

Only a runner session token can act, and only for its own conversation. Opening
a session and binding it to a turn need the operator switch and both Hermes
capabilities; unbinding and closing need neither, so revocation always works,
also after Hermes is switched off. A Hermes token cannot reach these
routes: get_current_user_or_session rejects it before any handler runs.
"""

from typing import Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.authorization_dependencies import authorization_subject
from src.core.authorization_types import AuthorizationSubject
from src.core.dependencies import get_service_db_session
from src.core.hermes_auth import require_hermes_enabled
from src.schemas.hermes import HermesSessionTokenResponse
from src.services.hermes_access import (
    HermesAccessDenied,
    active_hermes_turn,
    require_hermes_capabilities,
)
from src.services.session_manager import session_manager
from src.services.hermes_sessions import (
    HermesSessionError,
    bind_turn,
    close_session,
    current_session,
    open_session,
    unbind_turn,
)

router = APIRouter()

_STATUS = {
    "hermes_session_not_found": status.HTTP_404_NOT_FOUND,
    "hermes_session_closed": status.HTTP_409_CONFLICT,
    "hermes_session_conflict": status.HTTP_409_CONFLICT,
    "hermes_correlation_invalid": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class BindTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    turn_id: UUID = Field(alias="turnId")
    # The registered execution acting in the turn; its platform tool calls
    # over the MCP bridge are operations of this execution.
    execution_id: UUID = Field(alias="executionId")
    workflow_run_id: UUID | None = Field(default=None, alias="workflowRunId")
    workflow_phase: Literal["supervisor_prepare", "node", "supervisor_synthesize"] | None = Field(
        default=None, alias="workflowPhase"
    )
    workflow_node_key: str | None = Field(
        default=None, alias="workflowNodeKey", pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )


def _runner(user: UserInfo) -> tuple[AuthorizationSubject, UUID]:
    if user.raw_claims.get("type") != "session":
        raise HTTPException(status_code=403, detail="Only runner session tokens are allowed")
    return authorization_subject(user), UUID(str(user.raw_claims["conversation_id"]))


def _fail(exc: HermesAccessDenied | HermesSessionError) -> NoReturn:
    code = exc.code
    raise HTTPException(
        status_code=_STATUS.get(code, status.HTTP_403_FORBIDDEN), detail={"code": code}
    ) from None


@router.post(
    "/hermes/sessions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_hermes_enabled)],
)
async def open_hermes_session(
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> HermesSessionTokenResponse:
    subject, conversation_id = _runner(user)
    try:
        await require_hermes_capabilities(session, subject)
        row, issued = await open_session(
            session,
            subject,
            conversation_id,
            generation=session.info["conversation_fence"][2],
        )
    except (HermesAccessDenied, HermesSessionError) as exc:
        _fail(exc)
    await session.commit()
    return HermesSessionTokenResponse(
        token=issued.token,
        sessionId=str(row.id),
        expiresAt=issued.expires_at.isoformat(),
    )


class CurrentSessionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(serialization_alias="sessionId")
    expires_at: str = Field(serialization_alias="expiresAt")


@router.get("/hermes/sessions/current", dependencies=[Depends(require_hermes_enabled)])
async def current_hermes_session(
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> CurrentSessionResponse:
    """The open session, without rotating its token; 404 when there is none."""
    subject, conversation_id = _runner(user)
    row = await current_session(session, subject, conversation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "hermes_session_not_found"})
    return CurrentSessionResponse(session_id=str(row.id), expires_at=row.expires_at.isoformat())


@router.post("/hermes/turns/{turn_id}/touch", status_code=status.HTTP_204_NO_CONTENT)
async def touch_hermes_turn(
    turn_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> Response:
    """Keep the runner of a long Hermes turn: refresh its idle time and, when
    stale, its token, as a chat request does. Only for the conversation's
    active turn."""
    subject, conversation_id = _runner(user)
    try:
        turn = await active_hermes_turn(session, subject, conversation_id)
    except HermesAccessDenied as exc:
        _fail(exc)
    if turn.id != turn_id:
        _fail(HermesAccessDenied("hermes_turn_inactive"))
    await session.commit()
    await session_manager.get_or_create(
        str(conversation_id),
        user.user_id,
        user.org_id,
        generation=int(user.raw_claims.get("generation", 1)),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/hermes/sessions/{session_id}/turn",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_hermes_enabled)],
)
async def bind_hermes_turn(
    session_id: UUID,
    data: BindTurnRequest,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> Response:
    subject, conversation_id = _runner(user)
    try:
        await require_hermes_capabilities(session, subject)
        await bind_turn(
            session,
            subject,
            conversation_id,
            session_id,
            turn_id=data.turn_id,
            execution_id=data.execution_id,
            workflow_run_id=data.workflow_run_id,
            workflow_phase=data.workflow_phase,
            workflow_node_key=data.workflow_node_key,
        )
    except (HermesAccessDenied, HermesSessionError) as exc:
        _fail(exc)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/hermes/sessions/{session_id}/turn", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_hermes_turn(
    session_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> Response:
    subject, conversation_id = _runner(user)
    try:
        await unbind_turn(session, subject, conversation_id, session_id)
    except HermesSessionError as exc:
        _fail(exc)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/hermes/sessions/{session_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_hermes_session(
    session_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> Response:
    subject, conversation_id = _runner(user)
    try:
        await close_session(session, subject, conversation_id, session_id)
    except HermesSessionError as exc:
        _fail(exc)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
