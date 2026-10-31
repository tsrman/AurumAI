"""Previews of the apps Hermes Agent runs in its sandbox (docs/HERMES_AGENT.md).

The owner asks for a one-time ticket that opens a preview at its own origin;
the preview service, with its own secret, exchanges the ticket for the
preview's grant and checks that grant on every new connection. Every answer is
the preview's live state: the conversation's generation, both Hermes
capabilities, the owner and the session that shared it.
"""

import hmac
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, security
from src.core.authorization_dependencies import authorization_subject
from src.core.config import settings
from src.core.dependencies import get_org_db_session
from src.core.hermes_auth import hermes_db_session, require_hermes_enabled
from src.services import hermes_previews
from src.services.hermes_access import HermesAccessDenied, require_hermes_capabilities
from src.services.hermes_previews import PreviewError

# How long the preview service may reuse an answer for new connections.
DECISION_MAX_AGE_SECONDS = 5
_LABEL = r"^[0-9a-f]{32}$"

router = APIRouter(dependencies=[Depends(require_hermes_enabled)])
internal_router = APIRouter(dependencies=[Depends(require_hermes_enabled)])


@router.post("/conversations/{conversation_id}/previews/{preview_id}/ticket")
async def preview_ticket(
    conversation_id: UUID,
    preview_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> dict[str, Any]:
    """The URL that opens the preview once, for the conversation's owner."""
    subject = authorization_subject(user)
    try:
        await require_hermes_capabilities(session, subject)
        url = await hermes_previews.issue_ticket(session, subject, conversation_id, preview_id)
    except (HermesAccessDenied, PreviewError) as exc:
        # One answer for a stranger's, a closed and an unknown preview.
        code = "hermes_preview_closed" if exc.code == "hermes_preview_closed" else "hermes_preview_unavailable"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": code}) from None
    return {"url": url}


class RedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: str = Field(pattern=_LABEL)
    ticket: str = Field(min_length=1, max_length=4096)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: str = Field(pattern=_LABEL)
    grant: str = Field(min_length=1, max_length=4096)


def _require_service(credentials: HTTPAuthorizationCredentials | None) -> None:
    secret = settings.HERMES_PREVIEW_SERVICE_SECRET.get_secret_value()
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not configured")
    presented = credentials.credentials if credentials else ""
    if not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credential")


def _deny(reason: str) -> dict[str, Any]:
    return {"allow": False, "reason": reason, "maxAgeSeconds": DECISION_MAX_AGE_SECONDS}


def _allow(target: hermes_previews.PreviewTarget, **extra: Any) -> dict[str, Any]:
    return {
        "allow": True,
        "sessionId": target.session_id.hex,
        "port": target.port,
        "maxAgeSeconds": DECISION_MAX_AGE_SECONDS,
        **extra,
    }


async def _decide(claims: dict[str, Any], redeem: bool) -> dict[str, Any]:
    user = hermes_previews.claims_user(claims)
    try:
        async with hermes_db_session(user) as session:
            await require_hermes_capabilities(session, authorization_subject(user))
            if redeem:
                grant, target = await hermes_previews.redeem(session, claims)
                return _allow(target, grant=grant, grantMaxAgeSeconds=hermes_previews.GRANT_SECONDS)
            return _allow(await hermes_previews.check(session, claims))
    except (HermesAccessDenied, PreviewError) as exc:
        return _deny(exc.code)
    except HTTPException:
        # Identity or conversation fence refused: a deleted user, an archived
        # or replaced conversation.
        return _deny("hermes_preview_revoked")


@internal_router.post("/hermes/previews/redeem")
async def redeem_preview_ticket(
    data: RedeemRequest, credentials: HTTPAuthorizationCredentials | None = Depends(security)
) -> dict[str, Any]:
    _require_service(credentials)
    try:
        claims = hermes_previews.ticket_claims(data.ticket, data.preview)
    except PreviewError as exc:
        return _deny(exc.code)
    return await _decide(claims, redeem=True)


@internal_router.post("/hermes/previews/check")
async def check_preview_grant(
    data: CheckRequest, credentials: HTTPAuthorizationCredentials | None = Depends(security)
) -> dict[str, Any]:
    _require_service(credentials)
    try:
        claims = hermes_previews.grant_claims(data.grant, data.preview)
    except PreviewError as exc:
        return _deny(exc.code)
    return await _decide(claims, redeem=False)
