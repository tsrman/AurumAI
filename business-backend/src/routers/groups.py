import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import require_org_admin
from src.core.dependencies import get_org_db_session
from src.core.logging import logger
from src.models.group import Group
from src.models.user import User
from src.models.user_group import UserGroup
from src.repositories.group_repository import group_repository
from src.schemas.common import ApiResponse, Meta, Pagination
from src.schemas.group import (
    GroupCreateRequest,
    GroupMemberResponse,
    GroupMembersAddRequest,
    GroupMembersAddResponse,
    GroupResponse,
    GroupUpdateRequest,
)

router = APIRouter()


def _meta() -> Meta:
    return Meta(requestId=f"req_{uuid.uuid4().hex}", timestamp=datetime.now(timezone.utc).isoformat())


def _group_response(group: Group, member_count: int = 0) -> GroupResponse:
    return GroupResponse(
        id=str(group.id),
        orgId=str(group.org_id) if group.org_id else None,
        name=group.name,
        keycloakId=str(group.keycloak_id) if group.keycloak_id else None,
        lastSyncedAt=group.last_synced_at.isoformat() if group.last_synced_at else None,
        memberCount=member_count,
    )


async def _member_count(session: AsyncSession, group_id: UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(UserGroup).where(UserGroup.group_id == group_id)
    )
    return int(count or 0)


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
    member_count = (
        select(func.count())
        .select_from(UserGroup)
        .where(UserGroup.group_id == Group.id)
        .correlate(Group)
        .scalar_subquery()
    )
    result = await session.execute(
        select(Group, member_count).where(Group.org_id == org_id).order_by(Group.name)
    )
    return ApiResponse(
        data=[_group_response(group, int(count)) for group, count in result.all()],
        meta=_meta(),
    )


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
    return ApiResponse(
        data=_group_response(group, await _member_count(session, group.id)), meta=_meta()
    )


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


async def _own_group(session: AsyncSession, group_id: str, org_id: UUID) -> Group:
    group = await group_repository.get(session, group_id)
    if group is None or group.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/{group_id}/members")
async def list_group_members(
    group_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[GroupMemberResponse]]:
    org_id = _require_org_id(user)
    group = await _own_group(session, group_id, org_id)
    predicates = [UserGroup.group_id == group.id, User.org_id == org_id]
    needle = (search or "").strip()
    if needle:
        pattern = f"%{_escape_like(needle)}%"
        predicates.append(
            or_(
                User.email.ilike(pattern, escape="\\"),
                User.full_name.ilike(pattern, escape="\\"),
            )
        )
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(UserGroup)
            .join(User, User.id == UserGroup.user_id)
            .where(*predicates)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(User, UserGroup.source)
            .join(UserGroup, UserGroup.user_id == User.id)
            .where(*predicates)
            .order_by(func.lower(func.coalesce(User.full_name, User.email)), User.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return ApiResponse(
        data=[
            GroupMemberResponse(
                id=str(member.id),
                email=member.email,
                fullName=member.full_name,
                role=member.role or "member",
                status=member.status or "active",
                source=source,
            )
            for member, source in rows
        ],
        meta=_meta(),
        pagination=Pagination(hasMore=offset + len(rows) < total, total=total),
    )


@router.post("/{group_id}/members")
async def add_group_members(
    group_id: str,
    data: GroupMembersAddRequest,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[GroupMembersAddResponse]:
    org_id = _require_org_id(user)
    group = await _own_group(session, group_id, org_id)
    wanted = set(data.userIds)
    found = set(
        (
            await session.scalars(
                select(User.id).where(User.id.in_(wanted), User.org_id == org_id)
            )
        ).all()
    )
    if found != wanted:
        # A foreign or unknown id is refused as a whole; nothing is added.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    already = set(
        (
            await session.scalars(
                select(UserGroup.user_id).where(
                    UserGroup.group_id == group.id, UserGroup.user_id.in_(wanted)
                )
            )
        ).all()
    )
    new_ids = wanted - already
    session.add_all(
        UserGroup(user_id=user_id, group_id=group.id, source="manual") for user_id in new_ids
    )
    await session.commit()
    if new_ids:
        logger.info(
            "group_members_added",
            actor_id=user.user_id,
            org_id=str(org_id),
            group_id=str(group.id),
            user_ids=sorted(str(user_id) for user_id in new_ids),
        )
    return ApiResponse(
        data=GroupMembersAddResponse(added=len(new_ids), alreadyMembers=len(already)),
        meta=_meta(),
    )


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_member(
    group_id: str,
    user_id: UUID,
    user: UserInfo = Depends(require_org_admin),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    org_id = _require_org_id(user)
    group = await _own_group(session, group_id, org_id)
    membership = await session.scalar(
        select(UserGroup)
        .join(User, User.id == UserGroup.user_id)
        .where(
            UserGroup.group_id == group.id,
            UserGroup.user_id == user_id,
            User.org_id == org_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    if membership.source != "manual":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Manage synchronized memberships in Keycloak",
        )
    await session.execute(
        delete(UserGroup).where(
            UserGroup.group_id == group.id,
            UserGroup.user_id == user_id,
            UserGroup.source == "manual",
        )
    )
    await session.commit()
    logger.info(
        "group_member_removed",
        actor_id=user.user_id,
        org_id=str(org_id),
        group_id=str(group.id),
        user_id=str(user_id),
    )
