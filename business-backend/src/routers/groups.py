import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import require_org_admin
from src.core.dependencies import get_org_db_session
from src.models.group import Group
from src.repositories.group_repository import group_repository
from src.schemas.common import ApiResponse, Meta
from src.schemas.group import (
    GroupCreateRequest,
    GroupResponse,
    GroupUpdateRequest,
)

router = APIRouter()


def _meta() -> Meta:
    return Meta(requestId=f"req_{uuid.uuid4().hex}", timestamp=datetime.now(timezone.utc).isoformat())


def _group_response(group: Group) -> GroupResponse:
    return GroupResponse(
        id=str(group.id),
        orgId=str(group.org_id) if group.org_id else None,
        name=group.name,
        keycloakId=str(group.keycloak_id) if group.keycloak_id else None,
        lastSyncedAt=group.last_synced_at.isoformat() if group.last_synced_at else None,
    )


def _require_org_id(user: UserInfo) -> UUID:
    if not user.org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing org context")
    return UUID(user.org_id)


@router.get("/")
async def list_groups(
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[GroupResponse]]:
    org_id = _require_org_id(user)
    result = await session.execute(
        select(Group).where(Group.org_id == org_id).order_by(Group.name)
    )
    groups = result.scalars().all()
    return ApiResponse(data=[_group_response(g) for g in groups], meta=_meta())


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_group(
    data: GroupCreateRequest,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[GroupResponse]:
    org_id = _require_org_id(user)
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")

    existing = await group_repository.get_by_name_and_org(session, name, org_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Group '{name}' already exists"
        )

    group = Group(org_id=org_id, name=name)
    session.add(group)
    try:
        await session.flush()
    except IntegrityError:
        # The pre-check above is racy (two concurrent creates for the same
        # name both pass it) — uq_groups_org_id_name catches what it
        # misses; surface the same 409 either way instead of a raw 500.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Group '{name}' already exists"
        )
    await session.commit()
    await session.refresh(group)
    return ApiResponse(data=_group_response(group), meta=_meta())


@router.patch("/{group_id}")
async def update_group(
    group_id: str,
    data: GroupUpdateRequest,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[GroupResponse]:
    org_id = _require_org_id(user)
    group = await group_repository.get(session, group_id)
    # RLS already scopes session queries to the caller's org, but `.get()`
    # is a raw session.get() bypassing RLS-shaped predicates for a PK
    # lookup — double-check org ownership explicitly so a stray id from
    # another org 404s instead of leaking existence.
    if group is None or group.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    if data.name is not None:
        new_name = data.name.strip()
        if not new_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")
        if new_name != group.name:
            if group.last_synced_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot rename a group that has already synced from Keycloak",
                )
            duplicate = await group_repository.get_by_name_and_org(session, new_name, org_id)
            if duplicate is not None and duplicate.id != group.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Group '{new_name}' already exists",
                )
            group.name = new_name

    await session.commit()
    await session.refresh(group)
    return ApiResponse(data=_group_response(group), meta=_meta())


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: str,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    org_id = _require_org_id(user)
    group = await group_repository.get(session, group_id)
    if group is None or group.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    if group.last_synced_at is not None or group.keycloak_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Manage synchronized groups in Keycloak",
        )
    await session.delete(group)
    await session.commit()
