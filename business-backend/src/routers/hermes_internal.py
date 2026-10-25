"""Runner-only issuance of Hermes session tokens (docs/HERMES_AGENT.md).

Only a runner session token can mint, for its own conversation, while that
conversation has an active turn of the same user who holds both Hermes
capabilities. A Hermes token cannot mint another: it is rejected by
get_current_user_or_session before this handler runs.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.authorization_dependencies import authorization_subject
from src.core.dependencies import get_service_db_session
from src.core.hermes_auth import require_hermes_enabled
from src.schemas.hermes import HermesSessionTokenResponse
from src.services.hermes_access import (
    HermesAccessDenied,
    active_hermes_turn,
    require_hermes_capabilities,
)
from src.services.hermes_session_token import hermes_session_token_service

router = APIRouter(dependencies=[Depends(require_hermes_enabled)])


@router.post("/hermes/session-tokens", status_code=status.HTTP_201_CREATED)
async def issue_hermes_session_token(
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> HermesSessionTokenResponse:
    if user.raw_claims.get("type") != "session":
        raise HTTPException(status_code=403, detail="Only runner session tokens are allowed")
    subject = authorization_subject(user)
    conversation_id = UUID(str(user.raw_claims["conversation_id"]))
    try:
        await require_hermes_capabilities(session, subject)
        await active_hermes_turn(session, subject, conversation_id)
    except HermesAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code}
        ) from None
    issued = hermes_session_token_service.issue(
        org_id=subject.org_id,
        user_id=subject.user_id,
        conversation_id=conversation_id,
        generation=session.info["conversation_fence"][2],
    )
    return HermesSessionTokenResponse(
        token=issued.token,
        sessionId=str(issued.session_id),
        expiresAt=issued.expires_at.isoformat(),
    )
