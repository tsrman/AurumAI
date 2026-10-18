from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceRef,
    ResourceType,
)
from src.services.authorization import authorization_service


def _subject(
    org_id: str | None,
    user_id: str | None,
    role: str,
) -> AuthorizationSubject | None:
    try:
        return AuthorizationSubject(
            user_id=UUID(user_id or ""),
            org_id=UUID(org_id or ""),
            role=role,
        )
    except ValueError:
        return None


async def _allowed_model_ids(
    session: AsyncSession,
    subject: AuthorizationSubject | None,
    model_ids: list[str],
) -> list[str]:
    if subject is None:
        return []
    canonical_ids: list[str] = []
    for model_id in model_ids:
        try:
            ResourceRef(ResourceType.MODEL, model_id)
        except ValueError:
            continue
        canonical_ids.append(model_id)
    accessible = await authorization_service.accessible_resource_ids(
        session,
        subject,
        ResourceType.MODEL,
        AccessPermission.READ,
        canonical_ids,
    )
    return [model_id for model_id in canonical_ids if model_id in accessible]


async def allowed_model_ids(
    session: AsyncSession,
    org_id: str | None,
    model_ids: list[str],
    *,
    user_id: str | None = None,
    role: str = "member",
) -> list[str]:
    """Filter gateway model IDs through the authoritative resource grants."""
    return await _allowed_model_ids(
        session,
        _subject(org_id, user_id, role),
        model_ids,
    )


async def ensure_model_allowed(
    session: AsyncSession,
    org_id: str | None,
    model_id: str,
    *,
    user_id: str | None = None,
    role: str = "member",
) -> None:
    visible = await allowed_model_ids(
        session,
        org_id,
        [model_id],
        user_id=user_id,
        role=role,
    )
    if model_id not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Model not allowed",
        )
