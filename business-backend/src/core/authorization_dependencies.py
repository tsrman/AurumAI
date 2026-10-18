from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import AuthorizationSubject
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session
from src.services.authorization import authorization_service


def authorization_subject(user: UserInfo) -> AuthorizationSubject:
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


async def require_org_admin(
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    if user.role != "org_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


def require_capability(
    capability: Capability,
) -> Callable[..., Awaitable[UserInfo]]:
    async def checker(
        user: UserInfo = Depends(get_current_user),
        session: AsyncSession = Depends(get_org_db_session),
    ) -> UserInfo:
        subject = authorization_subject(user)
        if not await authorization_service.has_capability(
            session,
            subject,
            capability,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing capability: {capability.value}",
            )
        return user

    return checker
