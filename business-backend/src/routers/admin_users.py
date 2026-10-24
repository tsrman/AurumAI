import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import require_org_admin
from src.core.dependencies import get_current_org_id, get_org_db_session
from src.schemas.admin_users import AdminUserResponse
from src.schemas.common import ApiResponse, Meta, Pagination
from src.services.admin_user_directory import (
    AdminUserDirectory,
    AdminUserRecord,
    admin_user_directory,
)


router = APIRouter()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def get_admin_user_directory() -> AdminUserDirectory:
    return admin_user_directory


def _response(row: AdminUserRecord) -> AdminUserResponse:
    return AdminUserResponse(
        id=row.id,
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        status=row.status,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
    )


@router.get("/users", response_model=ApiResponse[list[AdminUserResponse]])
async def list_admin_users(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search_query: str | None = Query(default=None, alias="search", max_length=200),
    user_status: str | None = Query(default=None, alias="status", max_length=20),
    group_id: UUID | None = Query(default=None, alias="groupId"),
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
    directory: AdminUserDirectory = Depends(get_admin_user_directory),
) -> ApiResponse[list[AdminUserResponse]]:
    page = await directory.list(
        session,
        org_id,
        limit=limit,
        offset=offset,
        search=search_query,
        status=user_status,
        group_id=group_id,
    )
    return ApiResponse(
        data=[_response(row) for row in page.items],
        meta=_meta(),
        pagination=Pagination(
            hasMore=offset + len(page.items) < page.total,
            total=page.total,
        ),
    )
