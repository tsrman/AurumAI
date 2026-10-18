from typing import Any, Optional
import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt.exceptions import PyJWTError, ExpiredSignatureError
from pydantic import BaseModel, Field
from src.core.keycloak import keycloak_client
from src.services.session_token import session_token_service

__all__ = ["security", "UserInfo", "get_current_user", "get_current_user_or_session"]

security = HTTPBearer(auto_error=False)


class UserInfo(BaseModel):
    user_id: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    org_id: Optional[str] = None
    org_name: Optional[str] = None
    role: str = "member"
    raw_claims: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> UserInfo:
    token = credentials.credentials if credentials else request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        payload = await keycloak_client.decode_token(token)
    except ExpiredSignatureError as exc:
        request.state.keycloak_rejection = await keycloak_client.rejection_diagnostics(token, exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except PyJWTError as exc:
        request.state.keycloak_rejection = await keycloak_client.rejection_diagnostics(token, exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except httpx.HTTPError:
        # JWKS reload failed (Keycloak unreachable) — distinct from "your
        # token is bad": a 503 tells the caller to retry, a 401 would look
        # like the credentials themselves are the problem.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth service unavailable"
        )

    return UserInfo(
        user_id=payload["sub"],
        email=payload.get("email"),
        full_name=payload.get("name"),
        org_id=payload.get("org_id"),
        org_name=payload.get("org_name"),
        role=payload.get("role", "member"),
        raw_claims=payload,
    )


async def get_current_user_or_session(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> UserInfo:
    """Authenticate via Keycloak JWT or a signed runner session token."""
    try:
        return await get_current_user(request, credentials)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise

    token = credentials.credentials if credentials else request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    try:
        payload = session_token_service.decode(token)
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session token expired"
        )
    except PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session token"
        )

    if (
        payload.get("type") != "session"
        or "permissions" in payload
        or not isinstance(payload.get("sub"), str)
        or not payload["sub"].strip()
        or not isinstance(payload.get("conversation_id"), str)
        or not payload["conversation_id"].strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session token"
        )

    requested_conversation_id = request.path_params.get(
        "conversation_id"
    ) or request.query_params.get("conversation_id")
    token_conversation_id = payload["conversation_id"]
    if requested_conversation_id and token_conversation_id != requested_conversation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session token does not match conversation",
        )

    # The Keycloak failure was an expected probe, not the cause of any later
    # authorization failure (e.g. an unknown/revoked session principal).
    request.state.keycloak_rejection = None
    return UserInfo(
        user_id=payload["sub"],
        email=None,
        full_name=None,
        org_id=payload.get("org_id"),
        org_name=None,
        role="member",
        raw_claims=payload,
    )
