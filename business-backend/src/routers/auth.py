import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import get_current_user, UserInfo
from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.core.dependencies import get_db, get_org_db_session
from src.services.identity_sync import ensure_identity_synced
from src.schemas.auth import TokenCallbackRequest, TokenRefreshRequest, UserInfoResponse
from src.schemas.common import ApiResponse, Meta
from src.services.authorization import authorization_service
from src.services.auth_service import auth_service

router = APIRouter()
logger = logging.getLogger(__name__)


def _meta() -> Meta:
    return Meta(requestId="req_", timestamp=datetime.now(timezone.utc).isoformat())


def _authorization_subject(user: UserInfo) -> AuthorizationSubject:
    try:
        return AuthorizationSubject(
            user_id=UUID(user.user_id),
            org_id=UUID(user.org_id or ""),
            role=user.role,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid organization context",
        ) from exc


@router.post("/callback")
async def auth_callback(
    request: TokenCallbackRequest,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tokens = await auth_service.exchange_code(
        request.code,
        request.code_verifier,
        request.redirect_uri,
    )
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")

    from src.core.keycloak import keycloak_client
    from src.core.auth import UserInfo

    payload = await keycloak_client.decode_token(access_token)
    user_info = UserInfo(
        user_id=payload["sub"],
        email=payload.get("email"),
        full_name=payload.get("name"),
        org_id=payload.get("org_id"),
        org_name=payload.get("org_name"),
        role=payload.get("role", "member"),
        raw_claims=payload,
    )
    # See database.py's _AppSession/after_begin — setting session.info here
    # (not calling set_config directly) is what makes the RLS org context
    # survive a mid-request connection-pool swap, including the one inside
    # sync_keycloak_user's own commit+refresh below. SET LOCAL semantics
    # (is_local=true in the after_begin hook) also mean this never needs
    # explicit clearing — it can't leak into whatever request reuses this
    # pooled connection next.
    if user_info.org_id:
        session.info["org_id"] = user_info.org_id
    if settings.IDENTITY_SYNC_ENABLED:
        await ensure_identity_synced(session, user_info)
    else:
        await auth_service.sync_keycloak_user(session, user_info)
        await session.commit()

    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=tokens.get("expires_in", 3600),
    )
    if refresh_token:
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=86400 * 7,
        )
    return {"data": {"status": "authenticated"}, "meta": _meta()}


@router.post("/refresh")
async def auth_refresh(
    request: TokenRefreshRequest,
    response: Response,
) -> dict[str, Any]:
    tokens = await auth_service.refresh_tokens(request.refresh_token)
    response.set_cookie(
        key="access_token",
        value=tokens["access_token"],
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=tokens.get("expires_in", 3600),
    )
    return {"data": {"status": "refreshed"}, "meta": _meta()}


@router.post("/logout")
async def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"data": {"status": "logged_out"}, "meta": _meta()}


@router.get("/me", response_model=ApiResponse[UserInfoResponse])
async def auth_me(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[UserInfoResponse]:
    capabilities = await authorization_service.effective_capabilities(
        session,
        _authorization_subject(user),
    )
    return ApiResponse(
        data=UserInfoResponse(
            user_id=user.user_id,
            email=user.email,
            full_name=user.full_name,
            org_id=user.org_id,
            org_name=user.org_name,
            role=user.role,
            capabilities=sorted(capabilities, key=lambda item: item.value),
        ),
        meta=_meta(),
    )
