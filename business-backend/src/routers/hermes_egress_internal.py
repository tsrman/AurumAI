"""Per-connection checks for hermes-egress (docs/HERMES_AGENT.md).

hermes-egress authenticates with its own service secret and forwards the Hermes
session token the sandbox presented. The answer is the session's live state,
checked exactly as for a completion: identity, the conversation fence, both
Hermes capabilities, the session row and its bound turn. The proxy then applies
the effective mode's host rules itself. Rate limiting and the audit journal skip
this path: it is called per connection, and hermes-egress keeps its own
per-session limits and journal.
"""

import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from src.core.auth import security
from src.core.authorization_dependencies import authorization_subject
from src.core.config import settings
from src.core.hermes_auth import hermes_db_session, hermes_session_user, require_hermes_enabled
from src.services.hermes_access import HermesAccessDenied, require_hermes_capabilities
from src.services.hermes_egress import effective_egress_mode
from src.services.hermes_sessions import authorize_token

# How long hermes-egress may reuse an answer for new connections.
DECISION_MAX_AGE_SECONDS = 5

router = APIRouter(dependencies=[Depends(require_hermes_enabled)])


class EgressAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=4096)


def _require_service(credentials: HTTPAuthorizationCredentials | None) -> None:
    secret = settings.HERMES_EGRESS_SERVICE_SECRET.get_secret_value()
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not configured")
    presented = credentials.credentials if credentials else ""
    if not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credential")


def _deny(reason: str) -> dict[str, Any]:
    return {"allow": False, "reason": reason, "maxAgeSeconds": DECISION_MAX_AGE_SECONDS}


@router.post("/hermes/egress/authorize")
async def authorize_egress(
    data: EgressAuthorizeRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    _require_service(credentials)
    try:
        user = hermes_session_user(data.token)
    except HTTPException:
        return _deny("hermes_token_invalid")
    subject = authorization_subject(user)
    try:
        async with hermes_db_session(user) as session:
            try:
                await require_hermes_capabilities(session, subject)
                row = await authorize_token(session, subject, user.raw_claims)
            except HermesAccessDenied as exc:
                return _deny(exc.code)
            mode = await effective_egress_mode(session, subject)
            return {
                "allow": True,
                "mode": mode,
                "orgId": str(subject.org_id),
                "sessionId": str(row.id),
                "turnId": str(row.active_turn_id),
                "maxAgeSeconds": DECISION_MAX_AGE_SECONDS,
            }
    except HTTPException:
        # Identity or conversation fence refused (deleted user, archived or
        # replaced conversation): nothing may leave for it.
        return _deny("hermes_session_revoked")
